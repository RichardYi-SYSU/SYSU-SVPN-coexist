# iOA SVPN 隧道机制研究与 TUN 类代理共存分析

> 研究对象：腾讯 iOA 零信任安全（macOS 客户端，`QQPCMgr` 壳 + `TPN/ztsmngn` 引擎）。
> 观察环境：macOS（Apple Silicon）+ Clash Party 1.9.6（mihomo v1.19.30，TUN 模式）。
> 方法：本机文件静态分析（二进制 strings、配置数据库、YAML/日志）+ 三次真实连接的运行日志还原。
> 本文不涉及任何服务端漏洞或绕权操作，全部行为均为客户端在本机可见的既定逻辑。

## TL;DR

1. iOA 的 SVPN 引擎**自带一个本地 HTTP 代理**，连接后始终监听 `127.0.0.1:12639`（日志：`[NGNClient] [proxy] [start] | listen=127.0.0.1:12639`）。校内流量的"放行清单"在引擎侧按 **域名/IP + 端口** 校验（可访问区域策略），因此只要把流量喂给这个端口，**不依赖它抢路由也能访问校内资源**。
2. 与 Clash 系（Verge / Party / mihomo）TUN 的冲突来自三件事：**抢默认路由**、**DnsGuard 锁系统 DNS**、**争抢 127.0.0.1:53**。
3. 客户端存在三种接入模式：`TUNPTAP`（TUN 全接管）/ `agent2mode`（本地代理，兼容老 agent）/ `WebProxy`（PAC）。哪种生效由服务端策略 `NGNModeConfig` 与客户端状态共同决定，同一台机器两次连接可能表现不同。
4. 共存的最优解：**让 Clash TUN 继续当家，校内流量通过覆写规则指向 iOA 的 12639 本地代理**（链式代理）。若 iOA 回到 TUNPTAP 模式，连接后用 `fix-route-after-svpn.sh` 把默认路由还给物理网卡即可，SVPN 隧道本身不断。
5. 端口白名单是引擎侧校验的：如某网段资源只放行 `80;443;22;3389;8080`，其它端口（如 OpenList 的 5244）会被网关静默丢弃，可用 `ssh -L` 借 22 端口绕过。

## 1. 客户端解剖

macOS 版 iOA 安装在 `/Applications/QQPCMgr.localized/QQPCMgr.app`（腾讯电脑管家壳，iOA SVPN 为其内置模块）：

```
QQPCMgr.app
├── Contents/MacOS/QQPCMgr                 # 主 UI（ObjC）
├── Contents/Resources/
│   ├── NGNMgr.app/MacOS/NGNMgr            # NGN 调度器：模式决策、策略下发、执行 ConfigPACProxy.sh
│   ├── QQPCMgrDaemon                      # setuid 守护（本地 IPC :58827）
│   ├── NetworkServiceMgr                  # setuid：路由/系统代理修改的执行者
│   ├── QQPCPolicyMgr.app                  # 策略/EDR/DLP 等 XPC 服务集合
│   ├── runmgr                             # LaunchDaemon (com.tencent.runmgr, 30s 保活)
│   ├── ConfigPACProxy.sh                  # 设置系统代理 127.0.0.1:12639 + PAC 的脚本
│   └── ConfigDNSServers.sh                # 设置系统 DNS 的脚本
├── Contents/TPN/                          # SVPN 隧道核心（Go）
│   ├── ztsmngn                            # 75MB 主引擎（fake-ip/netstack/策略/gRPC 上报）
│   ├── SmartVPNTool                       # 隧道工具（SPA、网关探测）
│   ├── DnsGuard                           # DNS 守护：锁定系统 DNS 并监控 /etc/resolv.conf
│   ├── env.yaml                           # ★ 明文引擎配置（端口/fake-ip/netstack）
│   ├── conf.yaml                          # 加密的运行配置
│   └── error.html                         # 资源被策略拒绝时的错误页
```

数据与日志（排障金矿）：

| 路径 | 内容 |
|---|---|
| `/Library/Application Support/QQPCiOA/QQPCMgrConfig.db` | SQLite；策略、模式、端口等全部键值（`Config(key,value)`） |
| `/Library/Application Support/com.tencent.smartvpn/{pref,restore}.yaml` | 引擎偏好 / 路由快照（断线重连恢复用） |
| `/Library/Logs/ztsmngn/com.tencent.smartvpn/` | `events.log`（启动时序）、`smartvpn_access.log`（逐连接 CSV）、`*.INFO.log`（glog 主日志） |
| `/Library/Logs/QQPCMgr/iOALog/dns_guard.log` | DnsGuard 的 DNS 锁定记录 |

## 2. 端口与服务清单（连接建立后）

| 监听 | 进程 | 用途 |
|---|---|---|
| `127.0.0.1:12639` | ztsmngn | **本地 HTTP 代理**（CONNECT），本研究的主角 |
| `127.0.0.1:12101` | ztsmngn | 引擎控制 API（客户端 UI 与引擎的通道） |
| `127.0.0.1:53`（尝试） | ztsmngn | 引擎 DNS；若被 mihomo 占用则退回绑 `192.168.255.10:53`，无害 |
| utun11 `192.168.255.10/24` | — | TUNPTAP 模式的虚拟网卡（网关 `192.168.255.1`，fake-ip `100.12.0.0/22`） |
| `127.0.0.1:54331/54332` | QQPCMgrDaemon | 管家本地 UI 服务，与隧道无关 |

`env.yaml` 关键配置（明文）：

```yaml
agentServer:      # "兼容agent2.0, 监听的http端口"
  enable: true
  ports: [12639]
fakeIP: "100.12.0.0/22"
udpProxyEnable: true
netstack:
  ip: "192.168.255.10"       # 虚拟网卡
  gateway: "192.168.255.1"
```

## 3. 接入模式与决策链

客户端二进制中出现三种模式枚举：`TUNPTAP`、`agent2mode`、`WebProxy`。

- 服务端策略（Policy162 `NGNModeConfig`）下发默认模式，实测值为 `TUNPTAP`（`Details.NGNDefaultMode: TUNPTAP`）；
- 引擎启动时打印 `[SetStrategy-PrecisionRoute] agent2mode: true/false` —— **这一行直接决定是否抢路由**；
- 客户端切换模式后会执行 `ConfigPACProxy.sh`（日志：`AgentHTTP changeMode %@ success and notify daemon excute ConfigPACProxy script`），把系统 Web/安全代理设为 `127.0.0.1:12639` + PAC `http://127.0.0.1:12639/proxy_ngn.pac`。

**同一台机器的实测**：2026-07-12 的连接是 TUNPTAP（建 utun11 + 抢默认路由 + DNS 锁定）；2026-08-29 的连接是 agent2mode（日志 `agent2mode: true`，带宽统计 `ProxyMode: PAC`；不仅没抢路由，反而清理了上次残留的 `0.0.0.0 → 192.168.255.1` 并恢复 en0 默认路由）。结论：**模式受服务端策略与客户端内部状态影响，行为要以每次连接的 events.log 为准**。

## 4. TUNPTAP 连接时序（日志逐行还原）

```
I route_darwin.go:194 [route][RestoreRouteFromFile] route delete 0.0.0.0 192.168.255.1   # 清理上次残留
I route_darwin.go:213 [route][RestoreRouteFromFile] route add default 192.168.2.1        # 先恢复物理网关
I tuntap.go:271     Start OpenTunDevice
I tuntap.go:293     Open device name:utun11, mtu:16384
I route_darwin.go:117 [route][AddDefaultRoute] route delete default                       # ★ 删掉物理默认路由
I route_darwin.go:124 [route][AddDefaultRoute] route add default -interface utun11        # ★ 默认路由指向隧道
I route_darwin.go:139 [route][AddDefaultRoute] route add default 192.168.2.1 -ifscope en0 # 给自己留一条绑定 en0 的回程路由
I serve.go:127      Start listen on :127.0.0.1:12639                                      # 本地代理(始终启动)
I start.go:68       smartagent listenPort port:12101
I [DNS] need to raise DnsGuard...                                                          # DNS 守护进程
I dns_guard: set 'SystemDnsServer' ... reset [10.8.8.8 10.8.4.4] dns server                # ★ 系统 DNS 锁定校内
```

三个"接管点"（也是与 TUN 代理冲突的根源）：

1. **默认路由**：`route delete default` + `add default -interface utun11`。注意 mihomo TUN（auto-route）用的是 `1/8、2/7、4/6…` 这类**超网路由**，比 default 更精确，普通流量仍会先进 mihomo TUN —— 真正互踩的是双方对 default 的维护与 DNS。
2. **系统 DNS**：DnsGuard（root 常驻，父进程死后自动还原）把系统 DNS 钉在校内 DNS 并监控 `/etc/resolv.conf`，谁改改谁。依赖系统解析的组件（如 mihomo 的 `default-nameserver` 若配的是系统 DNS）会被牵连。
3. **`127.0.0.1:53`**：引擎尝试绑 53 做本地 DNS，与 mihomo 的 `dns-hijack any:53` 冲突；引擎抢不到会退回 `192.168.255.10:53`，不致命。

断线重连的恢复依据是 `/Library/Application Support/com.tencent.smartvpn/restore.yaml`：

```json
{"AddRouteInfo":[{"DestIP":"0.0.0.0","Gateway":"192.168.255.1","Nic":"utun11",...},
                 {"DestIP":"0.0.0.0","Gateway":"192.168.2.1","Nic":"en0","Ifscope":true,...}],
 "DelRouteInfo":[{"DestIP":"0.0.0.0","Gateway":"192.168.2.1","Nic":"en0","Ifscope":false,...}],
 "LocalNic":"en0","LocalGateWay":"192.168.2.1"}
```

## 5. 本地代理 12639 的工作方式

- 形态：HTTP 代理（`GET`/`CONNECT`），PAC 模式下所有浏览器流量都先问 `http://127.0.0.1:12639/proxy_ngn.pac`，PAC 按资源清单决定"校内 → 12639，其余 → DIRECT"；
- 校验：连接时按 **目标（域名或 IP）+ 端口** 查可访问区域策略。策略粒度到"应用/资源"（每条含 `name`、`connaddr`、`connport`、网关 `smartgate_server` 等，见第 6 节）；
- 转发：校验通过后经智能网关（`ioa-<tenant>.access.gateway.tencentwsd.cn:9443`）进隧道到校内；
- 逐连接审计日志 `smartvpn_access.log` 会记录进程名、目标、协议、状态码；
- **局限**：HTTP CONNECT 只能转发 TCP。UDP（语音/游戏等）只能走 TUNPTAP 模式的 netstack。

关键推论：**12639 是策略合法的"隧道入口"，它不需要路由配合**。只要 iOA 在线，任何本地进程把它当代理用，就能访问策略放行的校内资源 —— 这就是整个共存方案的地基。

## 6. 可访问区域策略（accessiblearea）

存储：`QQPCMgrConfig.db` 表 `Config`，键名 `SmartVpnSetRuleManager.<tenant_id>.accessiblearea`（value 为 JSON BLOB，顶层是 `[[{...}]]`）。提取：

```bash
sqlite3 "/Library/Application Support/QQPCiOA/QQPCMgrConfig.db" \
  "select writefile('accessiblearea.json', value) from Config \
   where key like 'SmartVpnSetRuleManager.%.accessiblearea';"
```

条目结构（示例见 `data/accessiblearea.example.json`）：

| 字段 | 含义 |
|---|---|
| `type` | `domain` / `ip` |
| `connaddr` | 域名（支持 `*.example.edu.cn` 通配）或 IP/网段（支持 `a-b` 区间与 `;` 多值） |
| `connport` | 放行端口，如 `80;443;22;3389;8080` 或 `1;65535`（全端口） |
| `name` | 资源名（管理台里配的"应用"名） |
| `filterport` | 是否启用端口过滤 |
| `internaldirect` | 是否允许引擎直连（不走网关） |
| `smartgate_server` | 智能网关地址 |

引擎对非清单内目标（或清单外端口）的处理：**客户端 CONNECT 仍返回 200，随后的真实转发被网关静默丢弃**（表现为挂起超时而非拒绝），浏览器就会不停重试——排障时非常有迷惑性。

## 7. 与 Clash 系 TUN 的冲突面总结

| 冲突点 | iOA 行为 | 对 Clash 的影响 | 化解 |
|---|---|---|---|
| 默认路由 | `route add default -interface utunN` | 抢占/维护竞态；mihomo 自身流量回程依赖物理默认路由 | `fix-route-after-svpn.sh` 恢复 default（SVPN 有 ifscope 路由保底，隧道不断） |
| 系统 DNS | DnsGuard 锁定校内 DNS | 节点域名解析可能失败 | Clash 的 `proxy-server-nameserver`/上游用公网 DoH |
| `127.0.0.1:53` | 引擎尝试绑定 | 与 mihomo `dns-hijack` 冲突 | 引擎自动回退绑虚拟网卡 IP，通常无需处理 |
| 系统代理 | PAC 模式下 `networksetup` 设 12639 | 覆盖 Clash 的系统代理设置 | 有 TUN 在则无感；无 TUN 时二选一 |

## 8. 共存方案

### 方案 A（首选）：iOA 以 agent2/PAC 模式连接 + 覆写规则链式 12639

iOA 连接后不抢路由、只提供 12639（并设系统代理）。Clash TUN 照常接管全部流量；覆写把校内域名/网段的规则**前插**进规则表并指向 `iOA-SVPN`（`http 127.0.0.1:12639`）。这样：

- 浏览器等走系统代理的应用：PAC 让校内直接进 12639；
- ssh/curl 等 CLI（不理系统代理）：包进 Clash TUN 后由前插规则送进 12639；
- 其余流量照走 Clash 节点，google/github 一切正常。

### 方案 B：iOA 回到 TUNPTAP（全接管）时的补救

连接后立即把无作用域默认路由还给物理网卡（脚本自动化，见 `scripts/fix-route-after-svpn.sh`）：

```bash
sudo route -n delete default -interface utun11     # utun 编号以 route get default 为准
sudo route -n add default 192.168.2.1              # 物理网关
# 或常驻对抗看门狗: sudo ./fix-route-after-svpn.sh watch
```

原理：SVPN 自己有一条 `default <gw> -ifscope en0`（`restore.yaml` 可证），隧道回程不依赖被删的那条 default；mihomo 的超网路由不受影响。校内流量改走 12639（同方案 A 的规则）。

### 方案 C（不推荐）：两个 TUN 并存硬调路由

理论可行（mihomo 超网 + iOA default 事实上共存过），但双方看门狗互相重建路由，行为不可预期，不做为建议。

### 规则注入：覆写层而不是改订阅

订阅一更新直接编辑 profile 的改动就会丢，所以规则放进客户端的**覆写/扩展配置**层：

- **Clash Party**：YAML 覆写用 `"+rules"` / `"+proxies"` / `"+proxy-groups"`（Party 的 `deepMerge` 对带 `+` 前缀的数组键做**前插**，`rules+` 是后插、裸键是整段替换——由 app.asar 反编译确认）；JS 覆写与 Verge 通用，`function main(config){ ...; return config }`，vm 沙箱（无 require）。
- **Clash Verge Rev**：全局扩展配置 Merge（`prepend-rules` / `prepend-proxies` / `prepend-proxy-groups`）或 Script（同款 `main(config)`）。

## 9. 复现方法论（供其他学校/其他版本参考）

1. **确认本地代理端口**：`grep -a "listen" /Library/Logs/ztsmngn/com.tencent.smartvpn/events.log`；或 `env.yaml` 的 `agentServer.ports`；
2. **确认接入模式**：主日志 grep `PrecisionRoute` / `ProxyMode`；二进制 `strings ztsmngn | grep -E "TUNPTAP|agent2mode|WebProxy"`；
3. **提取资源清单**：第 6 节的 sqlite 命令（不同版本键名可能微调，先 `select key from Config` 浏览）；
4. **还原连接时序**：`events.log` + 主日志 grep `AddDefaultRoute|RestoreRoute|DnsGuard|networksetup`；
5. **客户端参数**：Clash Party 的覆写合并语义藏在 `app.asar`（`out/main/index.js` 的 `deepMerge`）；
6. **端到端验证**：`curl -x http://127.0.0.1:12639 -I https://<校内站>`；`nc -x 127.0.0.1:12639 -X connect <ip> <port>`。

## 10. 版本与时效性

- 以上行为观察于 2026 年中的 iOA macOS 客户端（QQPCMgr 壳，TPN 引擎 3.7.x 时代构建）；
- 服务端策略随时可能改变模式/端口/清单，**每次连接后建议看一眼 `events.log`**；
- 本文档只描述客户端在本机的既定行为，请遵守学校 IT 管理规定，勿用于越权访问。

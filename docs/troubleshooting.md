# Troubleshooting

症状速查表，详情见对应章节。所有命令均可直接复制执行。

| # | 症状 | 大概率原因 | 修复 |
|---|---|---|---|
| T1 | 开 iOA 后代理节点全挂/外网全断 | iOA 以 TUNPTAP 全接管，抢了默认路由 | [T1](#t1) |
| T2 | `ssh 校内机` 报 Connection closed | 流量命中了订阅的"私有网段直连"规则，从家宽直拨内网 | [T2](#t2) |
| T3 | 校内 Web 某端口超时（如 OpenList:5244），日志里反复重试 | 端口不在 iOA 资源白名单，网关静默丢弃 | [T3](#t3) |
| T4 | 校内域名解析失败 | 系统 DNS 被 DnsGuard 锁到校内 / fake-ip 干扰 | [T4](#t4) |
| T5 | 更新订阅后校内规则消失 | 规则直接写在订阅文件里，被更新覆盖 | [T5](#t5) |
| T6 | 覆写"看起来没生效" | 未注册/未勾选/执行报错 | [T6](#t6) |
| T7 | `12639` 无监听 | iOA 未连接或处于非常规模式 | [T7](#t7) |
| T8 | 「校园网」组选 iOA-SVPN 后全部超时 | iOA 掉线或模式变化 | [T8](#t8) |
| T9 | UDP 类服务（语音/游戏/部分客户端）不通 | HTTP CONNECT 代理只转发 TCP | [T9](#t9) |
| T10 | Clash 核心启动失败 | 覆写 YAML/JS 语法错误 | [T10](#t10) |
| T11 | 系统代理被来回改写 | Party 与 iOA 的 PAC 互相覆盖 | [T11](#t11) |
| T12 | 想确认当前是谁在"接管网络" | — | [T12](#t12) |

---

## T1 开 iOA 后代理节点全挂

**判断**：

```bash
route -n get default | grep interface
# en0   = 正常（方案A：PAC/agent2 模式，或已修复）
# utunN = iOA 正在 TUNPTAP 全接管
```

佐证（iOA 引擎日志出现 `AddDefaultRoute`）：

```bash
grep -a "AddDefaultRoute" /Library/Logs/ztsmngn/com.tencent.smartvpn/*.INFO.log | tail -4
```

**修复**：把无作用域默认路由还给物理网卡（SVPN 自己有 `-ifscope en0` 回程路由，隧道不断线）：

```bash
sudo scripts/fix-route-after-svpn.sh          # 修一次
sudo scripts/fix-route-after-svpn.sh watch    # 常驻巡检，对抗引擎看门狗重抢；Ctrl-C 退出
```

验证：`route -n get default` 回到 en0；`curl -s -o /dev/null -w '%{http_code}\n' https://www.google.com` 为 200。

## T2 ssh 校内机报 `Connection closed by <校内IP> port 22`

**原因**：mihomo 日志会显示 `match IP-CIDR(172.16.0.0/12) using 🎯 全球直连` 之类——订阅自带的私有网段直连规则排在了你的校内规则前面，于是 Clash 从家宽**直接**拨内网地址，拨号失败后关闭连接（ssh 把它显示为 Connection closed）。

**修复**：确认覆写规则是**前插**（规则表第一条应是 `DOMAIN-SUFFIX,...,校园网`）：

```bash
# Clash Party
curl -s --unix-socket /tmp/mihomo-party-501-*.sock http://localhost/rules | \
  python3 -c "import json,sys; rs=json.load(sys.stdin)['rules']; print(rs[0])"
# Verge Rev
curl -s http://127.0.0.1:9090/rules   # 端口/密钥按你的设置
```

若首条不对：检查覆写是否被勾选/全局（T6），以及 Party YAML 覆写是否用了 `"+rules"`（带加号）而不是 `rules:`（整段替换）。

**验证链路**：发起一次 ssh 后立刻看连接详情，应显示链路 `iOA-SVPN → 校园网`。

## T3 校内 Web 某端口超时（案例：OpenList 5244）

**典型现象**：mihomo 日志里 `198.18.0.1:61xxx --> 172.18.x.x:5244 match ... using 校园网[iOA-SVPN]` 反复刷屏——那不是端口扫描，是浏览器在重试；每次 CONNECT 在 iOA 引擎处返回 200，但真实转发被网关丢弃，于是挂起超时。

**原因**：iOA 资源策略是"目标 + 端口"双重白名单。例如某校对 `172.16.0.0/12` 只放行 `80;443;22;3389;8080`。

**确认**（对比放行端口 22 与问题端口 5244）：

```bash
nc -x 127.0.0.1:12639 -X connect -w 6 <校内IP> 22  </dev/null | head -c 60   # 能看到 SSH banner
nc -x 127.0.0.1:12639 -X connect -w 6 <校内IP> 5244 </dev/null | head -c 60  # 200 后挂起
tail -f /Library/Logs/ztsmngn/com.tencent.smartvpn/smartvpn_access.log       # 引擎侧逐连接记录
```

**修复（立即可用）**：借放行的 22 端口做本地转发，服务端在自身回环上连 5244，不再经过网关策略：

```bash
ssh -f -N -L 15244:127.0.0.1:5244 <你的校内ssh主机>
open http://127.0.0.1:15244          # 停止: pkill -f "15244:127.0.0.1:5244"
```

**修复（正规）**：找管理员把 `<IP>:<端口>`（或整个网段+端口）加为 iOA 资源，下次策略同步后 12639 直连即可。

## T4 校内域名解析失败

- 走 12639 代理的流量**不需要**本机解析（CONNECT 直接把域名交给 iOA 引擎，用校内 DNS 解析）；
- 若 iOA 在 TUNPTAP 模式，它会把系统 DNS 锁到校内并常驻守护（`/Library/Logs/QQPCMgr/iOALog/dns_guard.log`），退出 iOA 后自动还原；
- Clash 侧建议：`proxy-server-nameserver` 与上游用公网 DoH，避免节点域名解析被系统 DNS 状态牵连；fake-ip 模式下无需为校内域名做特殊 DNS 配置。

## T5 更新订阅后校内规则消失

直接编辑订阅文件（profiles/*.yaml）的改动会被订阅更新覆盖。把规则放进**覆写层**（Party 的覆写 / Verge 的全局扩展配置），见 `docs/usage.md` 第 3 节。覆写层不随订阅更新而变。

## T6 覆写"看起来没生效"

Clash Party 排查顺序：

1. `override.yaml` 里有没有你的条目、`ext` 与文件扩展名一致、`global: true`（或订阅编辑信息里勾选了它）；
2. 执行日志：JS 覆写的 console 输出在 `override/<id>.log`，应有 `injected ... rules` 与「脚本执行成功」；
3. 运行时配置是否包含规则：`grep -c 校园网 ~/Library/Application\ Support/mihomo-party/work/config.yaml`；
4. 最终以 mihomo API 为准（T2 的命令）。

> 注意：`PUT /configs` 热重载只是重新加载 `work/config.yaml`，**不会重新编译覆写**；改完覆写要重启 Clash Party（或切换一次订阅）才会重新编译。

Verge Rev：确认「全局扩展配置」中的 Merge/Script 处于启用状态，且订阅配置链里包含它。

## T7 `127.0.0.1:12639` 无监听

```bash
netstat -anv -p tcp | grep 12639
cat /Library/Logs/ztsmngn/com.tencent.smartvpn/events.log   # 看 [proxy] [start] 行
```

- iOA 未登录/未连接 → 打开 iOA 连接；
- 连接了但没有 12639 → 极少数部署改了端口：`grep -A3 agentServer .../TPN/env.yaml`，然后生成覆写时 `--port <实际端口>`。

## T8 「校园网」组选 iOA-SVPN 后全部超时

按链路逐段测：

```bash
curl -m 6 -x http://127.0.0.1:12639 -sI https://www.sysu.edu.cn | head -1   # 通=引擎OK，问题在iOA之外
```

- 这里超时 → iOA 掉线/被管理员强制下线，重连 iOA；
- 这里 200 → 问题在 Clash 侧，回到 T2/T6。

## T9 UDP 类服务不通

12639 是 HTTP CONNECT 代理，只转发 TCP。需要 UDP 的场景（部分正版客户端授权、语音、游戏）只能：

- 让 iOA 走 TUNPTAP 模式（由服务端策略决定），并配合 T1 的路由修复；
- 或对 TCP 部分继续用本方案。

## T10 Clash 核心启动失败

多半是覆写语法错（YAML 缩进/JS 报错）。恢复方法：

```bash
# Party: 删除/注释 override.yaml 中的条目后重启应用；或修正覆写文件语法
tail -50 ~/Library/Application\ Support/mihomo-party/logs/$(date +%F).log
```

JS 覆写可先本地预检（与 Party 沙箱同款约定）：

```bash
node -e "const fs=require('fs'),vm=require('vm'); \
  const r=vm.runInNewContext(fs.readFileSync('<覆写>.js','utf8')+'\nmain({proxies:[],\"proxy-groups\":[],rules:[]})'); \
  console.log('rules:', r.rules.length)"
```

## T11 系统代理被来回改写

iOA（PAC 模式）会把系统代理设为 `127.0.0.1:12639` + PAC；Clash 开启"系统代理"时又设回 `127.0.0.1:7890`，后启动者赢。处理：

- 保持 Clash **TUN 常开**，系统代理开关随意（TUN 在网络层接管，系统代理只是入口之一）；
- 或按需二选一：要 PAC 行为就关 Party 的系统代理开关。

## T12 快照：现在到底是谁在接管网络

```bash
route -n get default                 # default 归属: en0(物理) / utunN(谁的TUN)
netstat -rn -f inet | head -30       # mihomo 超网路由(198.18.0.1) / iOA 的 ifscope 路由
ifconfig -a | grep -E '^utun'        # 虚拟网卡清单
scutil --proxy                       # 系统代理(PAC?)当前指向
scutil --dns | grep -m3 nameserver   # 系统 DNS 现状
netstat -anv -p tcp | grep -E '12639|:53 '   # iOA 本地代理 / 53 端口归属
```

经验判读：`default → en0` + `198.18.0.1` 超网路由存在 = 方案 A 的理想状态；`default → utun11(192.168.255.10 网段)` = TUNPTAP 全接管，需要 T1 的修复。

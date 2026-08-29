# 使用文档

目标：**iOA SVPN 与 Clash 系代理（Clash Party / Clash Verge Rev，TUN 模式）同时在线**——
google/github 等外网走代理节点，校内资源（SSH、Web、数据库）走 iOA 隧道，互不干扰。

```
                     ┌──────────────────────────── 本机 ────────────────────────────┐
 浏览器/CLI ──► Clash TUN/mihomo ──┬── 规则: 校内域名/IP ──► iOA-SVPN(127.0.0.1:12639) ──► iOA 隧道 ──► 校内
                                   └── 其余流量 ──────────► 代理节点 ──────────────────► 外网
```

## 0. 前提条件

- macOS，已登录 iOA SVPN（学校统一身份认证）；
- Clash Party 或 Clash Verge Rev，开启 **TUN 模式**；
- 本地代理端口为 `12639`（几乎全部 iOA 部署的默认值，确认方法见第 4 节）。

## 1. 提取你学校的资源清单

覆写规则的数据源是 iOA 下发的「可访问区域」策略（本机缓存在 SQLite 里）。在装有 iOA 的机器上执行：

```bash
sqlite3 "/Library/Application Support/QQPCiOA/QQPCMgrConfig.db" \
  "select writefile('accessiblearea.json', value) from Config \
   where key like 'SmartVpnSetRuleManager.%.accessiblearea';"
```

> 找不到键名时先浏览：`sqlite3 <db> "select key from Config;" | grep -i accessible`。
> 结构参考 `data/accessiblearea.example.json`；原理见 `docs/research.md` 第 6 节。

本仓库 `overrides/` 里已内置中山大学（SYSU）的生成结果，SYSU 用户可直接跳到第 3 步。

## 2. 生成覆写文件

```bash
python3 tools/generate_override.py \
  --input accessiblearea.json \
  --outdir out \
  --name campus-ioa-svpn \
  --group-name "校园网" \
  --proxy-name "iOA-SVPN" \
  --port 12639
```

产出四个文件：`campus-ioa-svpn.yaml`（Clash Party 用）、`campus-ioa-svpn.js`（Party/Verge 通用）、
两个 txt 是中间清单。脚本会自动：

- 把 `*.domain` 通配转成 `DOMAIN-SUFFIX`；`a-b` IP 区间折叠成最小 CIDR；
- 把落在 `10/8、172.16/12、192.168/16` 的碎片折叠成一条超网规则（引擎侧会再次按策略校验，超配无害）；
- 排除环回/组播/保留段和 mihomo fake-ip 段（`198.18.0.0/15`）。

## 3. 导入代理客户端

### 3a. Clash Party（1.9.x 实测）

1. 左侧「覆写」→「新建 YAML」→ 命名如 `iOA-SVPN 校内分流` → 粘贴 `overrides/sysu-ioa-svpn.yaml` 全文 → 保存；
2. 左侧「订阅」→ 目标订阅「编辑信息」→「覆写」→ 勾选刚建的覆写（或把覆写设为**全局**，对所有订阅生效）；
3. 左下角重启核心 / 重启应用。

> 也可以不走 UI，手动放置文件并编辑 `~/Library/Application Support/mihomo-party/override.yaml`：
> ```yaml
> items:
>   - id: campus-ioa-svpn
>     name: iOA-SVPN 校内分流
>     type: local
>     ext: yaml          # 或 js
>     global: true       # 对所有订阅生效
>     updated: 1700000000000
> ```
> 并把覆写文件放到 `~/Library/Application Support/mihomo-party/override/<id>.yaml`。

### 3b. Clash Verge Rev

两种方式任选：

**Merge（YAML）**：订阅右键 →「编辑 Merge」/ 全局扩展配置，粘贴（键名与 Party 不同，注意是 `prepend-`）：

```yaml
prepend-rules:
  - DOMAIN-SUFFIX,sysu.edu.cn,校园网
  - IP-CIDR,10.0.0.0/8,校园网,no-resolve
  # ... 其余规则同 overrides/ 里的清单
prepend-proxies:
  - {name: iOA-SVPN, type: http, server: 127.0.0.1, port: 12639, udp: false}
prepend-proxy-groups:
  - {name: 校园网, type: select, proxies: [iOA-SVPN, DIRECT]}
```

**Script（JS）**：直接粘贴 `overrides/sysu-ioa-svpn.js` 全文（`main(config)` 约定与 Party 相同，通用）。

> Verge 各版本的 Merge 键名略有出入，以应用内「全局扩展配置」的说明为准；JS 方式无版本差异，优先推荐。

### 3c. 其它内核/客户端

没有覆写机制的客户端，把 `overrides/sysu-ioa-svpn.yaml` 中去掉 `+` 前缀的三个小节手工合并进配置的
`proxies` / `proxy-groups` / `rules` **头部**（必须排在私有网段直连与 `MATCH` 之前）。

## 4. 验证

按顺序逐项确认：

```bash
# 1) iOA 在线且本地代理在监听
lsof -nP -iTCP:12639 -sTCP:LISTEN          # 或 netstat -anv -p tcp | grep 12639

# 2) 本地代理确实能进隧道（HTTP 200 即通）
curl -m 8 -x http://127.0.0.1:12639 -sI https://www.sysu.edu.cn | head -1

# 3) 端口级校验（CONNECT 建立后应看到 SSH banner）
nc -x 127.0.0.1:12639 -X connect -w 6 <校内IP> 22 </dev/null | head -c 40

# 4) Clash 规则已前插生效（Party/Verge 的外部控制 API）
curl -s --unix-socket /tmp/mihomo-party-501-*.sock http://localhost/rules | \
  python3 -c "import json,sys; rs=json.load(sys.stdin)['rules']; \
  print('总数', len(rs), '| 首条', rs[0]['payload'], '->', rs[0]['proxy'])"

# 5) 默认路由归属（判断 iOA 是否在 TUNPTAP 全接管）
route -n get default | grep interface      # 期望 en0；若为 utunN 见排障文档 T1

# 6) 端到端
curl -s -o /dev/null -w '%{http_code}\n' https://www.google.com    # 200
ssh <你的校内主机>                                                  # 正常登录
```

## 5. 日常使用

| 场景 | 操作 |
|---|---|
| iOA 在线（任意地点） | 「校园网」组保持 `iOA-SVPN`（默认），校内/外网全自动分流 |
| iOA 退出后 | 「校园网」组切 `DIRECT`；在校园网内想直连时也切它 |
| iOA 回到 TUNPTAP 全接管（`route get default` 的 interface 变成 utunN） | `sudo scripts/fix-route-after-svpn.sh watch`，挂着即可 |
| 访问策略外端口的服务（如 OpenList:5244） | `ssh -f -N -L 15244:127.0.0.1:5244 <校内主机>`，然后访问 `http://127.0.0.1:15244`（原理见排障 T3） |

建议顺手在 Clash 的 DNS 设置里把 `proxy-server-nameserver` 配成公网 DoH（如 `https://223.5.5.5/dns-query`），
使节点域名解析不依赖系统 DNS——iOA 在 TUNPTAP 模式会把系统 DNS 锁定到校内，这是节点"失联"的常见原因。

## 6. 已知限制

1. **仅 TCP**：12639 是 HTTP 代理，UDP（部分客户端授权、语音、游戏）必须走 iOA 自己的 TUNPTAP 模式；
2. **端口白名单**：只有策略放行的"域名/IP + 端口"能过网关，其余端口静默丢弃（表现是超时）；
3. **规则是快照**：管理员调整资源清单后，需重新执行第 1-2 步再更新覆写；
4. **规则前插依赖客户端覆写语义**：Party 的 `+key`、Verge 的 `prepend-*` 各自不同，混用客户端时注意对照。

## 7. 还原/卸载

- Clash 侧：删除覆写（或取消勾选），重启核心即可，订阅本身未被改动；
- iOA 侧：退出 SVPN 后它会自动还原系统 DNS 与路由（DnsGuard 随父进程退出还原）；
- ssh 隧道：`pkill -f "15244:127.0.0.1:5244"`（按实际端口）。

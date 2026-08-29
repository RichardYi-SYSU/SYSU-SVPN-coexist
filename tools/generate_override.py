#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 iOA SVPN 客户端导出的「可访问区域」策略 JSON，生成 Clash Party / Clash Verge Rev 覆写文件。

Step 1  在装有 iOA 的 Mac 上提取策略（QQPCMgrConfig.db 中按租户存储）：

    sqlite3 "/Library/Application Support/QQPCiOA/QQPCMgrConfig.db" \
      "select writefile('accessiblearea.json', value) from Config \
       where key like 'SmartVpnSetRuleManager.%.accessiblearea';"

Step 2  生成覆写：

    python3 generate_override.py --input accessiblearea.json --outdir ./out

Step 3  按 docs/usage.md 把 out/<name>.yaml（或 .js）导入代理客户端。

产出:
  <outdir>/<name>.yaml         Clash Party YAML 覆写（"+key" 前插语法）
  <outdir>/<name>.js           Clash Party JS Override / Clash Verge Rev Script（main(config) 约定）
  <outdir>/<name>_domains.txt  校内域名清单
  <outdir>/<name>_ips.txt      非私网 IP 段清单（CIDR）
"""

import argparse
import ipaddress
import json
import sys
from pathlib import Path

# 默认排除：环回/链路本地/组播/保留段 + mihomo fake-ip 池
DEFAULT_EXCLUDES = [
    "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
    "198.18.0.0/15",   # mihomo fake-ip 默认池
]

# 私有网段自动折叠：清单里落在这些超网内的条目较多时，直接用一条超网规则代替
PRIVATE_SUPERNETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


def load_entries(path: Path):
    """读取 accessiblearea JSON，兼容顶层为 list 或 [list] 两种形态。"""
    data = json.loads(path.read_text())
    flat = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else data
    if not isinstance(flat, list):
        sys.exit(f"输入格式不对：{path} 应为资源条目数组（参考 data/accessiblearea.example.json）")
    return flat


def parse_entries(entries):
    """拆出域名与 IP 段。connaddr 支持 'a.b.c.d'、'a.b.c.d-e.f.g.h'、'x;x;x' 三种写法。"""
    doms, nets = set(), set()
    for e in entries:
        ca, t = e.get("connaddr", ""), e.get("type")
        if t == "domain":
            doms.add((ca[2:] if ca.startswith("*.") else ca).lower())
        elif t == "ip":
            for tok in ca.split(";"):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    if "-" in tok:
                        a, b = tok.split("-", 1)
                        nets.update(ipaddress.summarize_address_range(
                            ipaddress.ip_address(a.strip()), ipaddress.ip_address(b.strip())))
                    else:
                        nets.add(ipaddress.ip_network(tok, strict=False))
                except ValueError:
                    print(f"  [warn] 跳过无法解析的条目: {tok!r} ({e.get('name', '')})")
    return sorted(doms), nets


def fold_private(nets):
    """私有超网内的条目折叠为一条超网规则（引擎侧会再次按策略校验，超配无害）。"""
    extra_rules = []
    for sup in PRIVATE_SUPERNETS:
        s = ipaddress.ip_network(sup)
        inside = {n for n in nets if n.version == 4 and n.network_address in s}
        if inside:
            extra_rules.append(sup)
            nets -= inside
    return nets, extra_rules


def render_yaml(doms, cidrs, extra_rules, args):
    L = [
        f"# {args.comment_title}",
        "#",
        "# 由 tools/generate_override.py 生成；数据来源：本机 iOA 下发的「可访问区域」策略。",
        '# 语法："+key" 表示把数组【前插】到原配置同名数组之前 (Clash Party deepMerge 约定)，',
        "#       因此订阅自带的规则全部保留、且排在我们之后，订阅更新不会冲掉本文件。",
        f"# 使用前提：iOA SVPN 处于连接状态（本地代理 127.0.0.1:{args.port} 在监听）。",
        "# 若 iOA 以 TUNPTAP(全接管)模式连接，另需 scripts/fix-route-after-svpn.sh。",
        "",
        '"+proxies":',
        f"  - name: {args.proxy_name}",
        "    type: http                      # iOA NGN 引擎本地代理，HTTP CONNECT，仅 TCP(UDP 不通)",
        "    server: 127.0.0.1",
        f"    port: {args.port}",
        "    udp: false",
        "",
        '"+proxy-groups":',
        f"  - name: {args.group_name}",
        "    type: select",
        "    proxies:",
        f"      - {args.proxy_name}                # iOA 在线时选这个(默认)",
        "      - DIRECT                          # iOA 离线/在校园网内直连时选这个",
        "",
        '"+rules":',
    ]
    L += [f"  - DOMAIN-SUFFIX,{d},{args.group_name}" for d in doms]
    L += [f"  - IP-CIDR,{r},{args.group_name},no-resolve" for r in extra_rules]
    L += [f"  - IP-CIDR,{c},{args.group_name},no-resolve" for c in cidrs]
    return "\n".join(L) + "\n"


def render_js(doms, cidrs, extra_rules, args):
    rules = [f"DOMAIN-SUFFIX,{d},{args.group_name}" for d in doms]
    rules += [f"IP-CIDR,{r},{args.group_name},no-resolve" for r in extra_rules]
    rules += [f"IP-CIDR,{c},{args.group_name},no-resolve" for c in cidrs]
    return "\n".join([
        f"// {args.comment_title}",
        "//",
        "// 由 tools/generate_override.py 生成；约定: function main(config) { ...; return config }",
        "// Clash Party(JS Override) 与 Clash Verge Rev(Script) 都在沙箱中调用 main() 并使用返回值，",
        "// 因此禁止 require/外部 IO，数据全部内联。",
        f"// 使用前提：iOA SVPN 处于连接状态（本地代理 127.0.0.1:{args.port} 在监听）。",
        "",
        f"const CAMPUS_DOMAINS = {json.dumps(doms, ensure_ascii=False)};",
        "",
        f"const CAMPUS_RULES = {json.dumps(rules, ensure_ascii=False)};",
        "",
        "function main(config) {",
        "  // 幂等: 重复执行时先清掉旧条目",
        f'  config.proxies = (config.proxies || []).filter((p) => p.name !== "{args.proxy_name}");',
        f'  config.proxies.push({{ name: "{args.proxy_name}", type: "http", server: "127.0.0.1", port: {args.port}, udp: false }});',
        "",
        f'  config["proxy-groups"] = (config["proxy-groups"] || []).filter((g) => g.name !== "{args.group_name}");',
        f'  config["proxy-groups"].push({{ name: "{args.group_name}", type: "select", proxies: ["{args.proxy_name}", "DIRECT"] }});',
        "",
        "  // 前插规则: 必须排在订阅的私有网段直连/MATCH 规则之前才有机会命中",
        "  config.rules = CAMPUS_RULES.concat(config.rules || []);",
        "",
        f'  console.log("{args.proxy_name} override: injected", CAMPUS_RULES.length, "rules");',
        "  return config;",
        "}",
    ]) + "\n"


def main():
    ap = argparse.ArgumentParser(description="从 iOA 可访问区域策略生成 Clash 覆写")
    ap.add_argument("--input", default="accessiblearea.json", help="策略 JSON 路径")
    ap.add_argument("--outdir", default=".", help="输出目录")
    ap.add_argument("--name", default="campus-ioa-svpn", help="输出文件名前缀")
    ap.add_argument("--proxy-name", default="iOA-SVPN", help="注入的代理节点名")
    ap.add_argument("--group-name", default="校园网", help="注入的策略组名")
    ap.add_argument("--port", type=int, default=12639, help="iOA 本地代理端口")
    ap.add_argument("--comment-title", default="iOA SVPN 校内分流覆写", help="文件头标题")
    args = ap.parse_args()

    entries = load_entries(Path(args.input))
    doms, nets = parse_entries(entries)

    excludes = [ipaddress.ip_network(x) for x in DEFAULT_EXCLUDES]
    nets = {n for n in nets
            if not (n.version == 4 and any(n.overlaps(e) for e in excludes))}

    nets, extra_rules = fold_private(nets)
    cidrs = sorted(nets)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"{args.name}.yaml").write_text(render_yaml(doms, cidrs, extra_rules, args))
    (outdir / f"{args.name}.js").write_text(render_js(doms, cidrs, extra_rules, args))
    (outdir / f"{args.name}_domains.txt").write_text("\n".join(doms) + "\n")
    (outdir / f"{args.name}_ips.txt").write_text("\n".join(str(c) for c in cidrs) + "\n")

    print(f"域名 {len(doms)} 条 | 公网段 {len(cidrs)} 条 | 私网折叠 {extra_rules}")
    print(f"已写入 {outdir}/({args.name}.yaml, {args.name}.js, {args.name}_domains.txt, {args.name}_ips.txt)")


if __name__ == "__main__":
    main()

#!/bin/bash
# iOA SVPN(TUN 模式) 连接后，把被它抢走的默认路由还给物理网卡，
# 让 Clash Party 的 TUN 继续当家；SVPN 隧道本身不受影响（它有 ifscope 路由保底），
# 校内资源改走它自带的本地代理 127.0.0.1:12639。
#
# 用法（需要 root）:
#   sudo ./fix-route-after-svpn.sh          # 连接 SVPN 后执行一次
#   sudo ./fix-route-after-svpn.sh watch    # 常驻巡检（对抗 SVPN 看门狗把路由加回来）
#   EN_IF=en5 sudo -E ./fix-route-after-svpn.sh   # 有线/其他网卡时指定接口
set -u
EN_IF="${EN_IF:-en0}"

gw_of() {
  netstat -rn -f inet | awk -v i="$1" '$1=="default" && $NF==i {print $2; exit}'
}

fix_once() {
  local tun
  tun=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
  case "$tun" in
    utun*) ;;
    *) [ "${1:-}" != "watch" ] && echo "默认路由不在虚拟网卡上（当前: ${tun:-无}），无需修复"; return 0 ;;
  esac
  local gw
  gw=$(gw_of "$EN_IF")
  if [ -z "$gw" ]; then echo "取不到 $EN_IF 的网关，跳过"; return 1; fi
  route -n delete default -interface "$tun" >/dev/null 2>&1
  route -n add default "$gw" >/dev/null 2>&1 && \
    echo "$(date '+%H:%M:%S') 已恢复默认路由 → $gw ($EN_IF)。SVPN 隧道保持连接，校内资源走 127.0.0.1:12639"
}

if [ "${1:-}" = "watch" ]; then
  echo "巡检模式：每 2 秒检查一次默认路由，Ctrl-C 退出"
  while true; do fix_once watch; sleep 2; done
else
  fix_once
fi

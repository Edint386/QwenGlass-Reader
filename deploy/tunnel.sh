#!/usr/bin/env bash
# glassreader 的反向 SSH 隧道（2026-10-30 起在用；reader/deploy/README.md）：
#
#   服务器 127.0.0.1:18780  <==SSH==  电脑 127.0.0.1:8780（glassreader serve）
#   OpenResty 把 https://cast.example.com/reader/ 反代到服务器的 127.0.0.1:18780（deploy/1panel/reader.conf）。
#
# 复用 glasscast 的 glasscast/deploy/tunnel.sh（断线重连、ServerAlive 探活、保温、错误提示），只换端口和地址；
# 和 glasscast 的隧道是两条独立的 SSH 连接、两个进程，互不影响（重启一条不会断另一条）。
# 保温请求打 $PUBLIC_URL/healthz = https://cast.example.com/reader/healthz，走 reader.conf 的 upstream 池。
#
# 用法（在另一个终端标签页里，和 glassreader serve 一起常驻）：
#   reader/deploy/tunnel.sh                  # 默认参数
#   reader/deploy/tunnel.sh --keep-awake     # 其余参数原样传给 glasscast/deploy/tunnel.sh
#   reader/deploy/tunnel.sh --dry-run        # 只打印 ssh 命令
#
# 也可以不另起隧道，给 glasscast 的隧道多加一个转发：glasscast/deploy/tunnel.sh -- -R 127.0.0.1:18780:127.0.0.1:8780
# 但那样 reader 的 upstream 池没人保温（保温只打 glasscast 的 /healthz），第一次工具调用常要多等约 0.5 s，所以不推荐。
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
# glasscast 的 tunnel.sh 不在旁边时，用 GLASSCAST_TUNNEL 指过去
TUNNEL="${GLASSCAST_TUNNEL:-$HERE/../../glasscast/deploy/tunnel.sh}"
[ -x "$TUNNEL" ] || { echo "找不到 $TUNNEL（glasscast 的 deploy/tunnel.sh，可用 GLASSCAST_TUNNEL 指定）" >&2; exit 1; }

exec "$TUNNEL" \
    --local-port "${READER_PORT:-8780}" \
    --remote-port "${READER_REMOTE_PORT:-18780}" \
    --public-url "${READER_PUBLIC_URL:-https://cast.example.com/reader}" \
    "$@"

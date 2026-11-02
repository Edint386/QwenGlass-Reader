#!/usr/bin/env bash
# 把代码发到云端并重建容器。在 Mac 上运行，位置不限。不碰 .env、书库和进度。
# 测试不过不发；容器等不到健康（compose.yaml 的 healthcheck）就报错退出，不然坏版本也算发布成功。
set -eu

HOST="${READER_CLOUD_HOST:-ubuntu@server.example.com}"
ROOT=/opt/glassreader
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

(cd "$REPO" && uv run pytest -q)

rsync -a --delete --exclude __pycache__ --rsync-path='sudo rsync' \
    "$REPO/glassreader" "$REPO/pyproject.toml" "$REPO/uv.lock" "$HERE/Dockerfile" "$HOST:$ROOT/app/"
rsync -a --rsync-path='sudo rsync' "$HERE/compose.yaml" "$HOST:$ROOT/"
ssh "$HOST" "sudo docker compose -f $ROOT/compose.yaml up -d --build --wait --wait-timeout 90"
# 被替换的旧镜像成了悬空镜像，不清的话每次发布都占一份磁盘
ssh "$HOST" "sudo docker image prune -f"

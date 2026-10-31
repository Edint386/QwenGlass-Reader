#!/usr/bin/env bash
# 把 Agent 文字版发到云端并重建它的容器。在 Mac 上运行，位置不限。不碰 .env、书库、进度和页图版实例。
set -eu

HOST="${READER_CLOUD_HOST:-ubuntu@server.example.com}"
ROOT=/opt/glassreader-text
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)

rsync -a --delete --exclude __pycache__ --rsync-path='sudo rsync' \
    "$REPO/glassreader" "$REPO/pyproject.toml" "$REPO/uv.lock" "$HERE/Dockerfile" "$HOST:$ROOT/app/"
rsync -a --rsync-path='sudo rsync' "$HERE/compose.yaml" "$HOST:$ROOT/"
ssh "$HOST" "sudo docker compose -f $ROOT/compose.yaml up -d --build"

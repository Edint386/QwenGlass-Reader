#!/usr/bin/env bash
# 把电脑书库里的 TXT 传到云端（只增改，不删云端的书）。传完不用重启，下次打开书架就能看到。
# 用法：deploy/cloud/sync_books.sh [书库目录，默认 ~/Books]
set -eu

HOST="${READER_CLOUD_HOST:-ubuntu@server.example.com}"
BOOKS="${1:-$HOME/Books}"

rsync -rt --include='*/' --include='*.[tT][xX][tT]' --exclude='*' --rsync-path='sudo rsync' \
    "$BOOKS/" "$HOST:/opt/glassreader/books/"
# 文件权限沿用源文件，属主是 root：0600 的书容器用户（10001）读不了
ssh "$HOST" "sudo chmod -R a+rX /opt/glassreader/books"

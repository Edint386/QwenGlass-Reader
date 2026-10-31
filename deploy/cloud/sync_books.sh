#!/usr/bin/env bash
# 把电脑书库里的 TXT 传到云端（只增改，不删云端的书）。传完不用重启，下次打开书架就能看到。
# 用法：deploy/cloud/sync_books.sh [书库目录，默认 ~/Books]
set -eu

HOST="${READER_CLOUD_HOST:-ubuntu@server.example.com}"
BOOKS="${1:-$HOME/Books}"

rsync -rt --include='*/' --include='*.txt' --exclude='*' --rsync-path='sudo rsync' \
    "$BOOKS/" "$HOST:/opt/glassreader/books/"

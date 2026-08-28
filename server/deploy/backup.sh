#!/usr/bin/env bash
# MyRecord 服务端简单备份：把整个 data 空间打 tar 到备份目录，保留最近 N 份。
# 用法（在独立 server 工程内）：server/deploy/backup.sh [备份目录] [保留份数]
set -euo pipefail

# server/ 已成为独立工程；脚本位于 server/deploy/，工程根即脚本上一级。
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DATA_DIR="$(grep -E '^\s*data_dir:' "$REPO_DIR/config.yaml" | awk '{print $2}' | tr -d '"')"
if [[ "$CONFIG_DATA_DIR" == ./* || "$CONFIG_DATA_DIR" != /* ]]; then
  DATA_DIR="$REPO_DIR/${CONFIG_DATA_DIR#./}"
else
  DATA_DIR="$CONFIG_DATA_DIR"
fi

BACKUP_DIR="${1:-$REPO_DIR/backups}"
KEEP="${2:-7}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/MyRecord-data-$STAMP.tar.gz"

tar -czf "$OUT" -C "$(dirname "$DATA_DIR")" "$(basename "$DATA_DIR")"
echo "已备份: $OUT"

# 只保留最近 KEEP 份
ls -1t "$BACKUP_DIR"/MyRecord-data-*.tar.gz 2>/dev/null | tail -n +"$((KEEP+1))" | xargs -r rm -f
echo "备份完成（保留最近 $KEEP 份）。"
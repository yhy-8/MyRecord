#!/usr/bin/env bash
# AgentRecord 服务端恢复：从备份 tar 恢复整个 data 空间。
# 用法（在独立 server 工程内）：server/deploy/restore.sh <备份文件.tar.gz>
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "用法: $0 <备份文件.tar.gz>" >&2
  exit 2
fi
BACKUP="$1"
if [[ ! -f "$BACKUP" ]]; then
  echo "备份文件不存在: $BACKUP" >&2
  exit 2
fi

# server/ 已成为独立工程；脚本位于 server/deploy/，工程根即脚本上一级。
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DATA_DIR="$(grep -E '^\s*data_dir:' "$REPO_DIR/config.yaml" | awk '{print $2}' | tr -d '"')"
if [[ "$CONFIG_DATA_DIR" == ./* || "$CONFIG_DATA_DIR" != /* ]]; then
  DATA_DIR="$REPO_DIR/${CONFIG_DATA_DIR#./}"
else
  DATA_DIR="$CONFIG_DATA_DIR"
fi

echo "警告：将用 $BACKUP 覆盖数据目录 $DATA_DIR"
read -r -p "确认恢复？(yes/no): " confirm
if [[ "$confirm" != "yes" ]]; then
  echo "已取消。"
  exit 1
fi

mkdir -p "$DATA_DIR"
# 解压后替换 data 目录内容
tar -xzf "$BACKUP" -C "$(dirname "$DATA_DIR")"
echo "恢复完成：$DATA_DIR"
echo "请重启服务后核对：sudo systemctl restart agentrecord-server"
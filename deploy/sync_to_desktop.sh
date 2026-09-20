#!/usr/bin/env bash
# sync_to_desktop.sh — 把仓库镜像到桌面阅读副本（说明/ + 代码/ 两目录，幂等）
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DST="${DESKTOP_SYNC_DST:-/mnt/c/Users/LIAN/Desktop/financial-crawler-20260908}"

rm -rf "$DST/说明" "$DST/代码"
mkdir -p "$DST/代码" "$DST/说明/指南" "$DST/说明/台账与文档" \
         "$DST/说明/Dify文档" "$DST/说明/日志"

rsync -a --delete \
  --exclude='data/' \
  --exclude='logs/' \
  --exclude='.vscode/' \
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.pyo' \
  --exclude='/*.md' \
  --exclude='deploy/*.md' \
  --exclude='drop_upload.py' \
  --exclude='start_kb_ui.sh' \
  --exclude='start_kb_ui_windows.cmd' \
  --exclude='dify_migration/README.md' \
  --exclude='dify_migration/docs/' \
  --exclude='dify_migration/.env.example' \
  --exclude='dify_migration/docker-compose.yml' \
  --exclude='dify_migration/nginx/' \
  --exclude='dify_migration/postgres/' \
  --exclude='dify_migration/volumes/' \
  "$SRC/" "$DST/代码/"

# 3) 文档与日志归类到“说明”
rsync -a "$SRC/deploy/USER_GUIDE_20260909.md" \
  "$DST/说明/指南/USER_GUIDE_20260909.md"
rsync -a "$SRC/deploy/PREDICT_VISUAL_20260909.md" "$DST/说明/指南/PREDICT_VISUAL_20260909.md"
rsync -a "$SRC/deploy/README.md" \
        "$SRC/deploy/req2_e2e_check_report_20260908.md" \
  "$DST/说明/台账与文档/"
for d in "$SRC/dify_migration/README.md" "$SRC/dify_migration/docs/"; do
  if [ -e "$d" ]; then rsync -a "$d" "$DST/说明/Dify文档/"; fi
done
rsync -a --delete "$SRC/logs/" "$DST/说明/日志/"
rsync -a "$SRC/TODO.md" "$DST/说明/TODO.md"

rsync -a "$SRC/deploy/README_DELIVERY_20260908.md" \
  "$DST/README_DELIVERY_20260908.md"

find "$DST" -mindepth 1 -maxdepth 1 \
  \( -name '代码' -o -name '说明' -o -name 'README_DELIVERY_*.md' \) \
  -prune -o -print0 | xargs -0 -r rm -rf

echo "已同步：$SRC -> $DST（说明/ + 代码/ 布局）"

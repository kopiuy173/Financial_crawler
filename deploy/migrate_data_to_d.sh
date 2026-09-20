#!/usr/bin/env bash
# migrate_data_to_d.sh — 把 data/ 与教材资料迁到 D 盘母目录（copy | remove | verify）
set -u

PROJ=/home/lian/projects/financial-crawler
SRC_DATA="$PROJ/data"
SRC_KB=/home/lian/projects/kb_data
DST='/mnt/d/数据库'
MODE="${1:-copy}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少命令: $1"; exit 2; }; }
need rsync; need md5sum

[ -d "$DST" ] || { echo "D 盘母目录不存在: $DST"; exit 1; }

migrate_copy() {
  set -e
  rsync -a "$SRC_DATA/knowledge.db"  "$DST/主数据库/knowledge.db"
  rsync -a "$SRC_DATA/chroma/"       "$DST/检索库/chroma/"
  rsync -a "$SRC_DATA/2026-09-07"    "$DST/新闻/"
  rsync -a "$SRC_DATA/2026-09-08"    "$DST/新闻/"
  rsync -a "$SRC_DATA/raw_html/"     "$DST/新闻/raw_html/"
  rsync -a "$SRC_DATA/raw/news/"     "$DST/原始归档/news/"
  rsync -a "$SRC_DATA/reports/"      "$DST/报告/"
  rsync -a "$SRC_DATA/cache/"        "$DST/缓存与状态/cache/"
  rsync -a "$SRC_DATA/tmp/"          "$DST/临时/tmp/"
  rsync -a "$SRC_DATA/upload_tmp/"   "$DST/临时/upload_tmp/"
  rsync -a "$SRC_KB/"                "$DST/教材与资料/" --exclude='*.pdf'
  rsync -a "$SRC_KB/"                "$DST/教材与资料/"
  echo "COPY_DONE"
}

migrate_remove() {
  set -e
  rm -rf "$SRC_DATA/knowledge.db" "$SRC_DATA/chroma" "$SRC_DATA/2026-09-07" \
         "$SRC_DATA/2026-09-08" "$SRC_DATA/raw_html" "$SRC_DATA/reports" \
         "$SRC_DATA/cache" "$SRC_DATA/raw"
  rm -rf "$SRC_KB"
  echo "REMOVE_DONE"
}

verify() {
  local ok=1
  cmp -s "$SRC_DATA/knowledge.db" "$DST/主数据库/knowledge.db" \
    && echo "OK knowledge.db" || { echo "DIFF knowledge.db"; ok=0; }
  local a b
  a=$(find "$SRC_DATA/2026-09-08" -type f | wc -l)
  b=$(find "$DST/新闻/2026-09-08" -type f | wc -l)
  [ "$a" = "$b" ] && echo "OK 新闻/2026-09-08 ($a)" || { echo "DIFF 新闻 2026-09-08 ($a vs $b)"; ok=0; }
  a=$(find "$SRC_DATA/raw/news" -type f | wc -l)
  b=$(find "$DST/原始归档/news" -type f | wc -l)
  [ "$a" = "$b" ] && echo "OK 原始归档/news ($a)" || { echo "DIFF 原始归档/news ($a vs $b)"; ok=0; }
  for f in "$SRC_KB"/*.pdf; do
    bf="$DST/教材与资料/$(basename "$f")"
    cmp -s "$f" "$bf" && echo "OK 教材 $(basename "$f")" || { echo "DIFF 教材 $(basename "$f")"; ok=0; }
  done
  [ -f "$DST/检索库/chroma/chroma.sqlite3" ] && echo "OK chroma.sqlite3" || { echo "DIFF chroma.sqlite3"; ok=0; }
  return $((1 - ok))
}

case "$MODE" in
  copy)   migrate_copy ;;
  remove) migrate_remove ;;
  verify) verify ;;
  *) echo "用法: $0 {copy|remove|verify}"; exit 1 ;;
esac

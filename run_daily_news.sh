#!/usr/bin/env bash
# run_daily_news.sh — 每日新闻抓取（窗口化；幂等，可重复执行）
# 手动补跑：bash run_daily_news.sh    日志：logs/daily_news.log
set -u

PROJ=/home/lian/projects/financial-crawler
PY=/home/lian/projects/ai_env/bin/python
LOG="$PROJ/logs/daily_news.log"
export DIFY_API_KEY="${DIFY_API_KEY:-}"
if [ -z "$DIFY_API_KEY" ] && [ -f "$PROJ/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROJ/.env"
  set +a
fi
[ -n "${DIFY_API_KEY:-}" ] || echo "[warn] 未配置 DIFY_API_KEY，本次将跳过 Dify 同步（见 .env.example）" >&2
export PIP_NO_CACHE_DIR=1

mkdir -p "$PROJ/logs"
echo "[$(date '+%F %T')] ===== 每日新闻任务开始 =====" >> "$LOG"
cd "$PROJ" || { echo "!! 无法进入 $PROJ" >> "$LOG"; exit 1; }

echo "--- 调度：执行所有已到期窗口（含 Dify 晚间同步）---" >> "$LOG"
"$PY" "$PROJ/news_scheduler.py" --once >> "$LOG" 2>&1
rc=$?

echo "[$(date '+%F %T')] ===== 每日新闻任务结束（rc=$rc）=====" >> "$LOG"
exit $rc


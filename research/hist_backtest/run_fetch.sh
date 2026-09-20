#!/usr/bin/env bash
# run_fetch.sh — 历史回放第 1 步：多分片并行抓取（新浪 2 片 + 腾讯 2 片）
set -u
cd /home/lian/projects/financial-crawler
export KB_STORE_ROOT=/mnt/d/数据库
PY=/home/lian/projects/ai_env/bin/python
W=/mnt/d/数据库/临时/hist_backtest
mkdir -p "$W"

for i in 0 1; do
  setsid nohup "$PY" research/hist_backtest/fetch_daily_hist.py \
    --days 131 --members-only --shards 4 --shard "$i" --source sina \
    >> "$W/fetch_s${i}.out" 2>&1 < /dev/null &
done
for i in 2 3; do
  setsid nohup "$PY" research/hist_backtest/fetch_daily_hist.py \
    --days 131 --members-only --shards 4 --shard "$i" --source tencent \
    >> "$W/fetch_s${i}.out" 2>&1 < /dev/null &
done

sleep 3
echo "started shards: $(pgrep -fc fetch_daily_hist.py)"

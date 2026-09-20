#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fetch_daily_hist.py — 历史回放（路径 A）第 1 步：抓全市场个股日线到影子库"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
WORK = Path('/mnt/d/数据库/临时/hist_backtest')
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SHADOW_DB = WORK / 'shadow.db'
PROGRESS = WORK / 'progress.json'
PROD_DB = '/mnt/d/数据库/主数据库/knowledge.db'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
SINA = ('https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
        'CN_MarketData.getKLineData')
TX = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS stock_daily_hist (
    symbol     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    pct_chg    REAL,
    close      REAL,
    volume     REAL,
    source     TEXT DEFAULT '',
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_sdh_date ON stock_daily_hist(trade_date);
'''


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%F %T'), msg)
    print(line, flush=True)
    WORK.mkdir(parents=True, exist_ok=True)
    with open(WORK / 'fetch.log', 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def to_sina(sym):
    code, _, ex = str(sym).partition('.')
    ex = ex.upper()
    p = 'sh' if ex == 'SH' else 'sz' if ex == 'SZ' else 'bj'
    return p + code


def to_symbol(code6):
    c = str(code6)
    if c.startswith(('6', '9')):
        return c + '.SH'
    if c.startswith(('0', '3')):
        return c + '.SZ'
    if c.startswith(('4', '8')):
        return c + '.BJ'
    return c


def load_universe(members_only=False):
    """从生产库只读读取标的清单：默认全部 A 股，members_only 时只取板块成分。"""
    uri = 'file:' + PROD_DB + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    try:
        if members_only:
            rows = conn.execute('SELECT DISTINCT symbol FROM board_members '
                                'ORDER BY symbol').fetchall()
        else:
            rows = conn.execute('SELECT symbol FROM ticker_universe '
                                'ORDER BY symbol').fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r and r[0]]


def recent_trade_days(n):
    """从生产库 market_snapshot 取最近 N 个交易日（按 15:00 快照存在性）。"""
    uri = 'file:' + PROD_DB + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM market_snapshot "
            "WHERE slot='15:00' ORDER BY trade_date").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows][-n:]


def shadow_conn():
    WORK.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SHADOW_DB))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def recent_trade_days(n):
    """取最近 N 个交易日：优先用交易日历缓存，回退读缓存文件。"""
    days = []
    try:
        import market_snapshot as ms
        days = [str(d) for d in ms.load_trade_days()]
    except Exception as e:  # noqa: BLE001
        log('交易日历读取失败，改用缓存文件: %s' % str(e)[:80])
    if not days:
        p = Path('/mnt/d/数据库/缓存与状态/cache/trade_days.json')
        if p.exists():
            raw = json.loads(p.read_text(encoding='utf-8'))
            arr = raw.get('dates') if isinstance(raw, dict) else raw
            days = [str(d) for d in (arr or [])]
    days = [d for d in days if d <= '2026-09-11']
    return days[-n:]


def fetch_sina(sym, datalen=250, timeout=20):
    r = requests.get(SINA, params={'symbol': to_sina(sym), 'scale': 240,
                                   'ma': 'no', 'datalen': datalen},
                     headers={'User-Agent': UA,
                              'Referer': 'https://finance.sina.com.cn'},
                     timeout=timeout)
    return r.json() or []


def fetch_tencent(sym, datalen=250, timeout=20):
    s = to_sina(sym)
    r = requests.get(TX, params={'param': s + ',day,,,' + str(datalen) + ',qfq'},
                     headers={'User-Agent': UA, 'Referer': 'https://gu.qq.com'},
                     timeout=timeout)
    node = ((r.json().get('data') or {}).get(s) or {})
    arr = node.get('qfqday') or node.get('day') or []
    out = []
    for it in arr:
        try:
            out.append({'day': it[0], 'close': it[2], 'volume': it[5]})
        except (IndexError, TypeError):
            continue
    return out


def rows_for_symbol(sym, days, datalen=250, source='auto'):
    """返回该标的在目标交易日集合内的日线行（含 pct_chg）。"""
    order = (('tencent', 'sina') if source == 'tencent'
             else ('sina', 'tencent'))
    raw, src = None, 'fail'
    for name in order:
        for attempt in (1, 2):
            try:
                raw = (fetch_tencent(sym, datalen=datalen) if name == 'tencent'
                       else fetch_sina(sym, datalen=datalen))
                src = name
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3 * attempt)
        if raw:
            break
    if not raw:
        return [], 'fail'
    series = []
    for it in raw:
        try:
            series.append((str(it['day']), float(it['close']),
                           float(it.get('volume') or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    series.sort(key=lambda x: x[0])
    want = set(days)
    out = []
    for i in range(1, len(series)):
        d, c, v = series[i]
        if d not in want:
            continue
        prev = series[i - 1][1]
        if not prev:
            continue
        out.append((sym, d, (c / prev - 1.0) * 100.0, c, v, src))
    return out, src


def main(argv=None):
    ap = argparse.ArgumentParser(description='全市场个股日线抓取（影子库）')
    ap.add_argument('--days', type=int, default=131)
    ap.add_argument('--datalen', type=int, default=250)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--shards', type=int, default=1)
    ap.add_argument('--sleep', type=float, default=0.12)
    ap.add_argument('--source', default='auto',
                    choices=['auto', 'sina', 'tencent'])
    ap.add_argument('--members-only', action='store_true')
    ap.add_argument('--shard', type=int, default=0)
    ap.add_argument('--no-resume', action='store_true')
    args = ap.parse_args(argv)

    WORK.mkdir(parents=True, exist_ok=True)
    days = recent_trade_days(args.days)
    log('目标交易日 %d 个：%s ~ %s' % (len(days), days[0] if days else '-',
                                    days[-1] if days else '-'))
    symbols = load_universe(members_only=args.members_only)
    if args.shards > 1:
        symbols = symbols[args.shard::args.shards]
    if args.limit:
        symbols = symbols[:args.limit]
    log('标的数 %d（分片 %d/%d）'
        % (len(symbols), args.shard + 1, args.shards))

    prog = PROGRESS if args.shards == 1 else (
        WORK / ('progress_s%d.json' % args.shard))
    done = set()
    if not args.no_resume:
        for pf in (PROGRESS, prog):
            if pf.exists():
                try:
                    done |= set(json.loads(pf.read_text(encoding='utf-8'))
                                .get('done') or [])
                except Exception:  # noqa: BLE001
                    pass
    todo = [s for s in symbols if s not in done]
    log('已完成 %d，待抓 %d' % (len(done), len(todo)))

    conn = shadow_conn()
    failed, ok, rows_total = [], 0, 0
    try:
        for i, sym in enumerate(todo, start=1):
            rows, src = rows_for_symbol(sym, days, datalen=args.datalen,
                                        source=args.source)
            if src == 'fail' or not rows:
                failed.append(sym)
            else:
                ok += 1
                rows_total += len(rows)
                conn.executemany(
                    'INSERT OR REPLACE INTO stock_daily_hist '
                    '(symbol, trade_date, pct_chg, close, volume, source) '
                    'VALUES (?,?,?,?,?,?)', rows)
                conn.commit()
                done.add(sym)
            if i % 50 == 0:
                prog.write_text(json.dumps(
                    {'done': sorted(done), 'failed': failed,
                     'updated_at': datetime.now().isoformat(timespec='seconds')},
                    ensure_ascii=False), encoding='utf-8')
                log('进度 %d/%d  成功 %d 失败 %d 行数 %d'
                    % (i, len(todo), ok, len(failed), rows_total))
            time.sleep(max(0.0, args.sleep))
    finally:
        prog.write_text(json.dumps(
            {'done': sorted(done), 'failed': failed,
             'updated_at': datetime.now().isoformat(timespec='seconds')},
            ensure_ascii=False), encoding='utf-8')
        n = conn.execute('SELECT COUNT(*) c FROM stock_daily_hist').fetchone()[0]
        d = conn.execute('SELECT COUNT(DISTINCT trade_date) c FROM stock_daily_hist'
                         ).fetchone()[0]
        conn.close()
    log('完成：成功 %d 失败 %d 写入 %d 行；影子库合计 %d 行 / %d 个交易日'
        % (ok, len(failed), rows_total, n, d))
    return 0


if __name__ == '__main__':
    sys.exit(main())


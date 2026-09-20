#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_board_hist.py — 历史回放（路径 A）第 2 步：个股日线聚合成板块历史"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault('KB_STORE_ROOT', '/mnt/d/数据库')
import database  # noqa: E402

WORK = Path('/mnt/d/数据库/临时/hist_backtest')
SHADOW_DB = WORK / 'shadow.db'
PROD_DB = '/mnt/d/数据库/主数据库/knowledge.db'
REAL_DATES = ('2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS board_daily_hist (
    board_kind TEXT NOT NULL,
    board_code TEXT NOT NULL,
    board_name TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    pct_chg    REAL,
    members    INTEGER,
    source     TEXT DEFAULT 'synth_eq',
    PRIMARY KEY (board_kind, board_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_bdh_date ON board_daily_hist(trade_date);
'''


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%F %T'), msg)
    print(line, flush=True)
    WORK.mkdir(parents=True, exist_ok=True)
    with open(WORK / 'build.log', 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def load_members():
    uri = 'file:' + PROD_DB + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            'SELECT board_kind, board_code, board_name, symbol '
            'FROM board_members').fetchall()
    finally:
        conn.close()
    boards, sym_idx = {}, {}
    for kind, code, name, sym in rows:
        key = (kind, code, name)
        boards.setdefault(key, []).append(sym)
        sym_idx.setdefault(sym, []).append(key)
    return boards, sym_idx


def shadow_conn():
    database.init_db(str(SHADOW_DB))
    database.ensure_market_schema(str(SHADOW_DB))
    conn = sqlite3.connect(str(SHADOW_DB))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def main(argv=None):
    ap = argparse.ArgumentParser(description='个股日线聚合成板块历史')
    ap.add_argument('--min-members', type=int, default=3)
    args = ap.parse_args(argv)

    boards, sym_idx = load_members()
    log('板块数 %d，成分标的数 %d' % (len(boards), len(sym_idx)))
    conn = shadow_conn()
    dates = [r[0] for r in conn.execute(
        'SELECT DISTINCT trade_date FROM stock_daily_hist ORDER BY trade_date'
    ).fetchall()]
    log('个股日线覆盖 %d 个交易日' % len(dates))

    total_rows = 0
    for d in dates:
        quotes = {r[0]: r[1] for r in conn.execute(
            'SELECT symbol, pct_chg FROM stock_daily_hist WHERE trade_date=?',
            (d,)).fetchall() if r[1] is not None}
        acc = {}
        for sym, pct in quotes.items():
            for key in sym_idx.get(sym) or ():
                a = acc.setdefault(key, [0.0, 0])
                a[0] += pct
                a[1] += 1
        rows = [(k[0], k[1], k[2], d, round(v[0] / v[1], 6), v[1])
                for k, v in acc.items() if v[1] >= args.min_members]
        if rows:
            conn.executemany(
                'INSERT OR REPLACE INTO board_daily_hist '
                '(board_kind, board_code, board_name, trade_date, pct_chg, members) '
                'VALUES (?,?,?,?,?,?)', rows)
            conn.commit()
            total_rows += len(rows)
    log('板块历史写入 %d 行' % total_rows)

    snap = [{'trade_date': r[3], 'slot': '15:00', 'kind': r[0], 'code': r[1],
             'name': r[2], 'pct_chg': r[4],
             'extra': json.dumps({'source': 'synth_eq'}, ensure_ascii=False)}
            for r in conn.execute(
                'SELECT board_kind, board_code, board_name, trade_date, pct_chg '
                'FROM board_daily_hist').fetchall()]
    database.upsert_snapshot_rows(conn, snap)
    log('影子库 market_snapshot 写入 %d 行' % len(snap))

    cal = calibrate(conn)
    (WORK / 'board_hist_calibration.md').write_text(cal, encoding='utf-8')
    log('校准报告：%s' % (WORK / 'board_hist_calibration.md'))
    conn.close()
    return 0


def rank(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    for pos, idx in enumerate(order, start=1):
        out[idx] = float(pos)
    return out


def rank_corr(a, b):
    n = len(a)
    if n < 3:
        return 0.0
    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db_ = sum((y - mb) ** 2 for y in rb) ** 0.5
    return num / (da * db_) if da and db_ else 0.0


def calibrate(conn):
    """与生产库 4 天真实快照对比（等权合成 vs 真实行业板块）。"""
    prod = sqlite3.connect('file:' + PROD_DB + '?mode=ro', uri=True)
    lines = ['# 板块历史聚合校准（等权合成 vs 真实快照）', '',
             '| 日期 | 可比板块数 | 平均绝对误差(pp) | 方向一致率 | 排名相关 |',
             '|---|---|---|---|---|']
    for date in REAL_DATES:
        real = {r[0]: r[1] for r in prod.execute(
            "SELECT code, pct_chg FROM market_snapshot WHERE trade_date=? AND "
            "slot='15:00' AND kind='industry_board'", (date,)).fetchall()}
        synth = {r[0]: r[1] for r in conn.execute(
            "SELECT board_code, pct_chg FROM board_daily_hist WHERE "
            "trade_date=? AND board_kind='industry_board'", (date,)).fetchall()}
        common = [k for k in real if k in synth and real[k] is not None
                  and synth[k] is not None]
        if not common:
            lines.append('| %s | 0 | - | - | - |' % date)
            continue
        diffs = [abs(synth[k] - real[k]) for k in common]
        same = sum(1 for k in common if (synth[k] >= 0) == (real[k] >= 0))
        mae = sum(diffs) / len(diffs)
        rc = rank_corr([synth[k] for k in common], [real[k] for k in common])
        lines.append('| %s | %d | %.3f | %.1f%% | %.3f |'
                     % (date, len(common), mae, 100.0 * same / len(common), rc))
    prod.close()
    lines += ['',
              '说明：合成为成分股等权平均；真实取自 market_snapshot 的板块涨跌幅；',
              '排名相关为 Spearman 系数（越接近 1 表示合成榜单顺序越贴近真实）。']
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    sys.exit(main())


#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""replay_predict.py — 历史回放（路径 A）第 3 步：影子库逐日规则版回放"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WORK = Path('/mnt/d/数据库/临时/hist_backtest')
SHADOW_DB = WORK / 'shadow.db'
(WORK / 'reports').mkdir(parents=True, exist_ok=True)

os.environ['KB_DB_PATH'] = str(SHADOW_DB)
os.environ.setdefault('KB_STORE_ROOT', '/mnt/d/数据库')
os.environ['PREDICT_STATE'] = str(WORK / 'replay_predict_state.json')
os.environ['PREDICT_LOG'] = str(WORK / 'replay_predict.log')
os.environ['PREDICT_REPORTS_DIR'] = str(WORK / 'reports')
os.environ['BASELINE_REPORTS_DIR'] = str(WORK / 'reports')

import predict as pr            # noqa: E402
import predict_baseline as pb   # noqa: E402


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%F %T'), msg)
    print(line, flush=True)
    with open(WORK / 'replay.log', 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {'n': 0}
    out = {'n': len(vals), 'mean': round(statistics.mean(vals), 4),
           'median': round(statistics.median(vals), 4)}
    if len(vals) > 1:
        out['sd'] = round(statistics.pstdev(vals), 4)
    return out


def paired(a, b):
    """配对比较：a 与 b 的逐日差值统计（忽略缺值日）。"""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return {'n': 0}
    diffs = [x - y for x, y in pairs]
    win = sum(1 for d in diffs if d > 0)
    lose = sum(1 for d in diffs if d < 0)
    m = statistics.mean(diffs)
    out = {'n': len(pairs), 'mean_diff': round(m, 4), 'win': win, 'lose': lose,
           'tie': len(pairs) - win - lose}
    if len(diffs) > 1:
        sd = statistics.pstdev(diffs)
        out['sd_diff'] = round(sd, 4)
        out['t'] = round(m / (sd / len(diffs) ** 0.5), 3) if sd else None
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='影子库逐日规则版回放')
    ap.add_argument('--days', type=int, default=120)
    args = ap.parse_args(argv)

    cfg = pr.load_config()
    conn = pr.get_conn()
    try:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM market_snapshot WHERE slot='15:00' "
            "ORDER BY trade_date").fetchall()]
    finally:
        conn.close()
    dates = dates[-args.days:]
    log('回放交易日 %d 个：%s ~ %s' % (len(dates), dates[0], dates[-1]))

    rows = []
    for i, d in enumerate(dates, start=1):
        try:
            g = pr.generate_day(cfg, date=d, no_llm=True, force=True)
            s = pr.settle(cfg, date=d)
            b = pb.run_for_date(d, cfg=cfg, write_db=True, write_log=False)
        except Exception as e:  # noqa: BLE001
            log('  %s 回放失败: %s' % (d, str(e)[:120]))
            continue
        rows.append({
            'date': d, 'gen': g.get('action'), 'model': g.get('model'),
            'evaluated': s.get('evaluated'),
            'hit_l': (s.get('hit') or {}).get('leading'),
            'hit_r': (s.get('hit') or {}).get('risk'),
            'actual': b.get('actual'), 'mom_ind': b.get('momentum_ind'),
            'mom_con': b.get('momentum_con'), 'rev_ind': b.get('reversal_ind'),
            'rev_con': b.get('reversal_con')})
        if i % 10 == 0:
            log('进度 %d/%d（%s 完成）' % (i, len(dates), d))

    acts = [r['actual'] for r in rows]
    mi = [r['mom_ind'] for r in rows]
    mc = [r['mom_con'] for r in rows]
    ri = [r['rev_ind'] for r in rows]
    rc = [r['rev_con'] for r in rows]
    summary = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'days': len(rows), 'date_from': dates[0] if dates else None,
        'date_to': dates[-1] if dates else None,
        'note': '规则版回放（--no-llm）；板块历史为成分股等权合成；非正式验收',
        'rule_actual': stats(acts), 'momentum_industry': stats(mi),
        'momentum_concept': stats(mc), 'reversal_industry': stats(ri),
        'reversal_concept': stats(rc),
        'random_industry_expect': 0.2449, 'random_concept_expect': 0.0686,
        'paired_actual_vs_mom_ind': paired(acts, mi),
        'paired_actual_vs_rev_ind': paired(acts, ri),
        'paired_actual_vs_mom_con': paired(acts, mc),
        'rows': rows}
    (WORK / 'replay_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    md = ['# 历史回放（路径 A）汇总', '',
          '- 生成时间：' + summary['generated_at'],
          '- 区间：%s ~ %s（%d 个交易日）' % (summary['date_from'],
                                            summary['date_to'], summary['days']),
          '- 说明：' + summary['note'], '',
          '| 策略 | 样本天数 | 平均命中率 | 中位数 | 标准差 |',
          '|---|---|---|---|---|']
    for key, label in (('rule_actual', '规则版预测 混合'),
                       ('momentum_industry', '惯性 行业'),
                       ('momentum_concept', '惯性 概念'),
                       ('reversal_industry', '反转 行业'),
                       ('reversal_concept', '反转 概念')):
        s = summary[key]
        md.append('| %s | %s | %s | %s | %s |'
                  % (label, s.get('n'), s.get('mean'), s.get('median'),
                     s.get('sd', '-')))
    md += ['', '- 解析随机期望：行业 0.2449，概念 0.0686', '',
           '## 配对比较（规则版 减 基线）', '',
           '| 对比 | 天数 | 平均差 | 胜 | 负 | 平 | t 值 |',
           '|---|---|---|---|---|---|---|']
    for key, label in (('paired_actual_vs_mom_ind', '规则版 vs 惯性 行业'),
                       ('paired_actual_vs_rev_ind', '规则版 vs 反转 行业'),
                       ('paired_actual_vs_mom_con', '规则版 vs 惯性 概念')):
        p = summary[key]
        md.append('| %s | %s | %s | %s | %s | %s | %s |'
                  % (label, p.get('n'), p.get('mean_diff'), p.get('win'),
                     p.get('lose'), p.get('tie'), p.get('t', '-')))
    md += ['', '注意：板块历史为等权合成（与真实板块指数误差约 0.2 至 0.6 个百分点），',
           '且规则版不含新闻与 LLM；结论仅用于早期筛选，不代表正式验收。']
    (WORK / 'replay_summary.md').write_text('\n'.join(md) + '\n', encoding='utf-8')
    log('汇总已写入 %s' % (WORK / 'replay_summary.md'))
    print(json.dumps({k: summary[k] for k in
                      ('days', 'rule_actual', 'momentum_industry',
                       'paired_actual_vs_mom_ind')}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())


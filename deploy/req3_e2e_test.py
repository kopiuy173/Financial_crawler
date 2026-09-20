#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""需求3 E2E 冒烟测试：盘前生成→结算回填→复盘反思→参数指纹/回滚闭环。"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.chdir(PROJ)
sys.path.insert(0, str(PROJ))

_TMP_PARENT = PROJ / 'data' / 'tmp'
_TMP_PARENT.mkdir(parents=True, exist_ok=True)
TMP_ROOT = Path(tempfile.mkdtemp(prefix='req3_e2e_', dir=str(_TMP_PARENT))
                if not os.environ.get('REQ3_TEST_KEEP') else _TMP_PARENT / 'req3_keep')
TMP_ROOT.mkdir(parents=True, exist_ok=True)
TMP_DB = os.environ.get('KB_DB_PATH') or str(TMP_ROOT / 'req3.db')
TMP_CFG = TMP_ROOT / 'predict_e2e.yaml'
TMP_STATE = TMP_ROOT / 'predict_state.json'
TMP_LOG = TMP_ROOT / 'predict.log'
REPORT_DIR = str(TMP_ROOT / 'reports')
os.environ['KB_DB_PATH'] = TMP_DB
os.environ['PREDICT_CONFIG'] = str(TMP_CFG)
os.environ['PREDICT_STATE'] = str(TMP_STATE)
os.environ['PREDICT_LOG'] = str(TMP_LOG)
os.environ['PREDICT_REPORTS_DIR'] = REPORT_DIR
os.environ['MARKET_CACHE_DIR'] = str(TMP_ROOT / 'cache')

FAKE_CAL = ['2026-08-17', '2026-08-18', '2026-08-19', '2026-08-20', '2026-08-21',
            '2026-08-24', '2026-08-25', '2026-08-26', '2026-08-27', '2026-08-28']
B0, D0, D1, DMISS = FAKE_CAL[0], FAKE_CAL[1], FAKE_CAL[2], FAKE_CAL[3]

import yaml  # noqa: E402

import database as db  # noqa: E402
import market_snapshot as ms  # noqa: E402

ms.load_trade_days = lambda *a, **k: list(FAKE_CAL)
ms.trading_days_between = lambda start, end: [d for d in FAKE_CAL
                                              if start <= d <= end]
ms.is_trade_day = lambda dstr: dstr in FAKE_CAL
ms.next_trade_day = lambda dstr, strict=False: (None if not FAKE_CAL else
    next((d for d in FAKE_CAL if (d > dstr if strict else d >= dstr)), None))

import predict as pr  # noqa: E402

# 测试日志不污染 logs/predict.log
pr.log = lambda msg: print('  [predict] ' + str(msg), flush=True)


def _cfg_from_base():
    base = yaml.safe_load((Path(PROJ) / 'config' / 'predict.yaml').read_text(
        encoding='utf-8')) or {}
    cfg = copy.deepcopy(base)
    cfg['model']['enabled'] = False          # 离线：走规则基线
    cfg['params']['reflect']['min_eval_samples'] = 2
    cfg['params']['settle']['leading_hit_rank'] = 3
    cfg['params']['settle']['risk_hit_rank'] = 3
    cfg['params']['settle']['top_k_actual'] = 5
    cfg['morning']['due'] = '08:40'
    cfg['morning']['deadline'] = '09:10'
    cfg['morning']['allow_late_until'] = '09:30'
    TMP_CFG.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                       encoding='utf-8')
    return cfg


CFG = _cfg_from_base()
db.init_db(TMP_DB)   # 建全量表（含需求3 五张新表）

FAIL = []


def ok(name, cond, detail=''):
    mark = 'PASS' if cond else 'FAIL'
    if not cond:
        FAIL.append(name)
    print('  [' + mark + '] ' + name + (('  -- ' + str(detail)) if detail else ''))


def sec(title):
    print('\n===== ' + title + ' =====')


def put_boards(conn, date, items):
    rows = []
    for kind, code, name, pct in items:
        rows.append({'trade_date': date, 'slot': '15:00', 'kind': kind,
                     'code': code, 'name': name, 'close': 100.0 + pct,
                     'pct_chg': pct, 'volume': 0.0, 'amount': 0.0,
                     'extra': '{}'})
    db.upsert_snapshot_rows(conn, rows)


def industry_set(prefix, pairs):
    return [('industry_board', prefix + ('BK%03d' % i), name, pct)
            for i, (name, pct) in enumerate(pairs)]


def concept_set(prefix, pairs):
    return [('concept_board', 'CN' + prefix + ('%02d' % i), name, pct)
            for i, (name, pct) in enumerate(pairs)]


B0_INDUSTRY = [('I00', 3.0), ('I01', 2.8), ('I02', 2.6), ('I03', 2.4),
               ('I04', 2.2), ('I05', 2.0), ('I06', -0.5), ('I07', -0.8),
               ('I08', -1.0), ('I09', -1.5), ('I10', -2.4), ('I11', -2.6),
               ('I12', -2.9)]
B0_CONCEPT = [('j%02d' % i, v) for i, v in enumerate(
    [-1.8, -1.4, -1.0, -0.6, -0.2, 0.2, 0.6, 1.0])]

D0_INDUSTRY = [('I12', -3.0), ('I11', -2.0), ('I10', -1.5), ('I09', -1.0),
               ('I08', -0.6), ('I07', -0.2), ('I01', 0.3), ('I06', 0.4),
               ('I05', 0.9), ('I04', 1.4), ('I03', 1.6), ('I02', 2.5),
               ('I00', 3.1)]
D0_CONCEPT = [('c%02d' % i, -3.2 - 0.1 * i) for i in range(12)]
D1_INDUSTRY = [('I03', 1.9), ('I04', 1.5), ('I05', 1.0), ('I06', 0.6),
               ('I07', 0.2), ('I08', -0.3), ('I09', -0.8), ('I10', -1.3)]
D1_CONCEPT = [('x%02d' % i, -3.2 - 0.1 * i) for i in range(8)]


sec('1) 快照夹具 + 盘前规则基线生成（幂等）')
conn = db.get_connection(TMP_DB)
put_boards(conn, B0, industry_set('A', B0_INDUSTRY) + concept_set('A', B0_CONCEPT))
put_boards(conn, D0, industry_set('D', D0_INDUSTRY) + concept_set('D', D0_CONCEPT))
put_boards(conn, D1, industry_set('E', D1_INDUSTRY) + concept_set('E', D1_CONCEPT))
print('  basis B0 板块行数:',
      conn.execute("SELECT COUNT(*) c FROM market_snapshot WHERE trade_date=?",
                   (B0,)).fetchone()['c'])
conn.close()

g1 = pr.generate_day(CFG, date=D0, basis_date=B0)          # no_llm（模型 disabled）
ok('generate action=generated', g1['action'] == 'generated',
   json.dumps(g1, ensure_ascii=False))
conn = db.get_connection(TMP_DB)
pred = conn.execute("SELECT side, seq, theme, kind, board_code, status "
                    "FROM predictions WHERE trade_date=? ORDER BY side, seq",
                    (D0,)).fetchall()
themes = [(r['side'], r['theme']) for r in pred]
ok('规则基线：领涨3 = I00/I01/I02',
   themes[:3] == [('leading', n) for n in ('I00', 'I01', 'I02')], str(themes))
ok('规则基线：风险2 = I12/I11',
   themes[3:] == [('risk', n) for n in ('I12', 'I11')], str(themes))
ok('生成行均写 kind/status=pending/board_code',
   all(r['kind'] == 'industry_board' and r['status'] == 'pending'
       and r['board_code'] for r in pred))
g2 = pr.generate_day(CFG, date=D0, basis_date=B0)
cnt_d0 = conn.execute("SELECT COUNT(*) c FROM predictions WHERE trade_date=?",
                      (D0,)).fetchone()['c']
ok('重复生成幂等（不重建）', g2['action'] == 'existing' and cnt_d0 == 5)
g3 = pr.generate_day(CFG, date=D0, basis_date=B0, force=True)
cnt_d0 = conn.execute("SELECT COUNT(*) c FROM predictions WHERE trade_date=?",
                      (D0,)).fetchone()['c']
ok('force 重建且行数一致', g3['action'] == 'generated' and cnt_d0 == 5)
fp0 = conn.execute("SELECT params_fp FROM predictions WHERE trade_date=? LIMIT 1",
                   (D0,)).fetchone()['params_fp']
day0 = conn.execute('SELECT status, model, basis_date, params_fp FROM predict_days '
                    'WHERE trade_date=?', (D0,)).fetchone()
ok('predict_days 落 generated 行', day0 and day0['status'] == 'generated'
   and day0['model'] == 'rule' and day0['basis_date'] == B0
   and day0['params_fp'] == fp0)
conn.close()

sec('2) 结算回填 vs_outcome（evening 编排内部走 settle+report）')
res_eve = pr.evening_stage(CFG, date=D0, no_llm=True, no_auto=True)
sp = res_eve['settle']
ok('evening: settle action=settled', sp['action'] == 'settled',
   json.dumps({k: sp.get(k) for k in ('evaluated', 'hit', 'reversed',
                                      'missed', 'uncovered')}, ensure_ascii=False))
ok('结算 5 条已回填', sp['evaluated'] == 5)
ok('领涨 命中2/错判1', sp['hit']['leading'] == 2
   and sp['reversed']['leading'] == 1,
   json.dumps(sp.get('reversed'), ensure_ascii=False))
ok('风险 命中2/漏判0', sp['hit']['risk'] == 2 and sp['missed']['risk'] == 0)
ok('漏判=当日实际榜前5 未命中',
   sp['uncovered']['leading'] == ['I03', 'I04', 'I05']
   and len(sp['uncovered']['risk']) == 5,
   json.dumps(sp.get('uncovered'), ensure_ascii=False))
conn = db.get_connection(TMP_DB)
row = conn.execute("SELECT status, vs_outcome FROM predictions "
                   "WHERE trade_date=? AND theme='I01'", (D0,)).fetchone()
out = json.loads(row['vs_outcome'])
ok('I01 错判落库（rank7 且 reversed）',
   row['status'] == 'evaluated' and out['reversed'] is True
   and out['vs_rank'] == 7 and out['hit'] is False)
row2 = conn.execute("SELECT vs_outcome FROM predictions "
                    "WHERE trade_date=? AND theme='I00'", (D0,)).fetchone()
out2 = json.loads(row2['vs_outcome'])
ok('I00 命中且 vs_pct_chg=3.1', out2['hit'] is True and out2['vs_rank'] == 1
   and abs(out2['vs_pct_chg'] - 3.1) < 1e-9, str(out2))
day0b = conn.execute('SELECT status, settled_at FROM predict_days '
                     'WHERE trade_date=?', (D0,)).fetchone()
ok('predict_days 转 settled', day0b['status'] == 'settled'
   and day0b['settled_at'])
conn.close()
rep_json = Path(REPORT_DIR) / ('predict_eval_' + D0.replace('-', '') + '.json')
rep_md = Path(REPORT_DIR) / ('predict_eval_' + D0.replace('-', '') + '.md')
ok('日结报告 json/md 落盘',
   rep_json.exists() and rep_md.exists()
   and 'I00' in rep_md.read_text(encoding='utf-8'), str(rep_json))
ok('evening 复盘：1 天样本不足 → 不写反思行',
   len(res_eve['reflect'].get('rows', []) or []) == 0)

sec('3) 第二天结算 + 复盘滚动（样本够 → 反思行 + 漏判口径）')
g4 = pr.generate_day(CFG, date=D1, basis_date=B0)
ok('D1 规则生成', g4['action'] == 'generated')
s2 = pr.settle(CFG, date=D1)
ok('D1 结算 5 条全 missed',
   s2['action'] == 'settled' and s2['evaluated'] == 5
   and s2['hit'] == {'leading': 0, 'risk': 0}
   and s2['missed'] == {'leading': 3, 'risk': 2})
ok('D1 漏判 = 当日实际榜前5（预测未命中）',
   s2['uncovered']['leading'] == ['I03', 'I04', 'I05', 'I06', 'I07']
   and len(s2['uncovered']['risk']) == 5,
   json.dumps(s2.get('uncovered'), ensure_ascii=False))
conn = db.get_connection(TMP_DB)
r2 = pr.reflect(CFG, date=D1, no_llm=True, no_auto=True)
ok('复盘样本=2 ≥ min → 写反思行', len(r2['rows']) == 1
   and r2['rows'][0]['trade_date'] == D1, json.dumps(r2, ensure_ascii=False))
refl = conn.execute("SELECT summary, suggestions, applied, model FROM "
                    "predict_reflections WHERE trade_date=?", (D1,)).fetchone()
print('  reflection row:', json.dumps({'model': refl['model']}, ensure_ascii=False))
sug = json.loads(refl['suggestions'] or '{}')
ok('no_llm → 无 LLM 建议、无自动应用',
   refl['model'] == 'rule' and sug == {}
   and json.loads(refl['applied'])['applied'] == {})
stat = json.loads(refl['summary'])['stats']
ok('滚动统计 2 天：领涨 总6/命中2',
   stat['days'] == 2 and stat['per_side']['leading']['hit'] == 2
   and stat['per_side']['leading']['total'] == 6,
   json.dumps(stat, ensure_ascii=False))
conn.close()

sec('4) 有界参数建议 / 指纹历史 / 自动应用与回滚')
before_fp = pr.params_fp(pr.effective_params(CFG))
h0 = db.get_connection(TMP_DB).execute(
    'SELECT COUNT(*) c FROM predict_param_history').fetchone()['c']
bounded = pr._bounded({'generation.n_leading': 99, 'settle.leading_hit_rank': 18,
                       'settle.top_k_actual': 0, 'unknown.key': 5},
                      CFG.get('tunable_bounds') or {})
ok('建议白名单 + 边界钳制（越界钳制、非白名单丢弃）',
   bounded == {'generation.n_leading': 6},
   json.dumps(bounded, ensure_ascii=False))
ok('口径参数已冻结（不在自动调参白名单）',
   all(k not in (CFG.get('tunable_bounds') or {}) for k in
       ('settle.leading_hit_rank', 'settle.risk_hit_rank',
        'settle.top_k_actual')))
app1 = pr.apply_param_changes({'generation.n_leading': 5},
                              reason='req3 e2e', source='reflect')
conn = db.get_connection(TMP_DB)
eff1 = pr.effective_params(CFG)
ov = conn.execute('SELECT key, value FROM predict_params').fetchall()
ok('自动应用生效（fp 变化 + overrides 落表）',
   app1['new_fp'] != before_fp and len(ov) == 1
   and eff1['generation']['n_leading'] == 5,
   json.dumps({'fp': app1.get('new_fp'), 'changed': app1.get('changed')},
              ensure_ascii=False))
top = conn.execute('SELECT fp, parent_fp, source FROM predict_param_history '
                   'ORDER BY id DESC LIMIT 2').fetchall()
ok('历史链：after.parent=before_fp', top[0]['parent_fp'] == before_fp
   and top[1]['source'] == 'reflect' and h0 + 2
   <= conn.execute('SELECT COUNT(*) c FROM predict_param_history'
                   ).fetchone()['c'])
conn.close()
rb = pr.rollback_to(before_fp, reason='req3 e2e rollback')
conn = db.get_connection(TMP_DB)
ov2 = conn.execute('SELECT COUNT(*) c FROM predict_params').fetchone()['c']
eff2 = pr.effective_params(CFG)
conn.close()
ok('回滚到基线指纹', rb['new_fp'] == before_fp and ov2 == 0
   and pr.params_fp(eff2) == before_fp
   and eff2['settle']['leading_hit_rank'] == 3
   and eff2['generation']['n_leading'] == 3)

sec('5) tick：超过 09:30 兜底窗口 → missed（幂等；稳态空转不产出动作）')
_real_now = ms.now_local
ms.now_local = lambda cfg=None: datetime(2026, 8, 20, 9, 35, 0)  # DMISS 09:35
try:
    a1 = pr.tick(CFG, date=DMISS)
    ok('tick 首调：>09:30 记 missed', len(a1) == 1 and a1[0]['action'] == 'missed',
       json.dumps(a1, ensure_ascii=False))
    conn = db.get_connection(TMP_DB)
    c = conn.execute("SELECT COUNT(*) c FROM predictions WHERE trade_date=?",
                     (DMISS,)).fetchone()['c']
    conn.close()
    ok('missed 不生成预测行', c == 0)
    st = json.loads(Path(TMP_STATE).read_text(encoding='utf-8'))
    ok('状态记录 action=missed', st.get('morning', {}).get('action') == 'missed'
       and st.get('morning', {}).get('decided') is True)
    a2 = pr.tick(CFG, date=DMISS)
    ok('tick 稳态返回空动作（不重复 missed / --once 可退出）', a2 == [])
finally:
    ms.now_local = _real_now

sec('6) CLI 冒烟（params / status / rollback 前缀错误路径）')
env = dict(os.environ)
for label, argv in (('params', ['params']), ('status', ['status']),
                    ('generate', ['generate', '--date', D0, '--no-llm'])):
    r = subprocess.run([sys.executable, 'predict.py'] + argv, env=env,
                       capture_output=True, text=True, timeout=90)
    print('  --- predict.py ' + ' '.join(argv) + ' rc=' + str(r.returncode)
          + ' ---')
    print((r.stdout or r.stderr).strip()[:300])
    ok('CLI ' + label, r.returncode == 0)

print('\n' + '=' * 30)
if FAIL:
    print('FAILED ' + str(len(FAIL)) + ' 项: ' + str(FAIL))
    sys.exit(1)
print('ALL PASS  (临时库: ' + TMP_DB + ')')
if not os.environ.get('REQ3_TEST_KEEP'):
    import shutil
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
sys.exit(0)

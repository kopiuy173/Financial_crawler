#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""需求3 回归测试：锁死两个已定位并修复的缺陷，防止回退。"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from datetime import datetime

PROJ = Path(__file__).resolve().parent.parent
os.chdir(PROJ)
sys.path.insert(0, str(PROJ))

_TMP_PARENT = PROJ / 'data' / 'tmp'
_TMP_PARENT.mkdir(parents=True, exist_ok=True)
TMP_ROOT = Path(tempfile.mkdtemp(prefix='req3_reg_', dir=str(_TMP_PARENT)))
TMP_DB = str(TMP_ROOT / 'reg.db')
TMP_CFG = TMP_ROOT / 'predict_reg.yaml'
os.environ['KB_DB_PATH'] = TMP_DB
os.environ['PREDICT_CONFIG'] = str(TMP_CFG)
os.environ['PREDICT_STATE'] = str(TMP_ROOT / 'predict_state.json')
os.environ['PREDICT_LOG'] = str(TMP_ROOT / 'predict.log')
os.environ['PREDICT_REPORTS_DIR'] = str(TMP_ROOT / 'reports')

import yaml  # noqa: E402

import database as db  # noqa: E402
import model_interface as mi  # noqa: E402
import predict as pr  # noqa: E402
import predict_baseline as pb  # noqa: E402

_LOGGED = []
_real_log = pr.log


def _capture(msg):
    _LOGGED.append(str(msg))
    _real_log(msg)


pr.log = _capture

B, D1 = '2026-08-17', '2026-08-18'        # 依据日 / 预测目标日

FAIL = []


def ok(name, cond, detail=''):
    if not cond:
        FAIL.append(name)
    print('  [' + ('PASS' if cond else 'FAIL') + '] ' + name
          + (('  -- ' + str(detail)) if detail else ''))


def sec(title):
    print('\n===== ' + title + ' =====')


_base = yaml.safe_load((PROJ / 'config' / 'predict.yaml').read_text(
    encoding='utf-8')) or {}
_cfg = copy.deepcopy(_base)
_cfg['model']['enabled'] = True
_cfg['auto_tune']['enabled'] = False
TMP_CFG.write_text(yaml.safe_dump(_cfg, allow_unicode=True, sort_keys=False),
                   encoding='utf-8')
CFG = pr.load_config()
db.init_db(TMP_DB)

N = 20
NAMES = ['N%02d' % i for i in range(N)]


def _kind_code(i):
    return (('industry_board', 'BK%03d' % i) if i < 10
            else ('concept_board', 'CN%02d' % i))


def _rows(date, slot, pcts):
    out = []
    for i, pct in enumerate(pcts):
        kind, code = _kind_code(i)
        out.append({'trade_date': date, 'slot': slot, 'kind': kind,
                    'code': code, 'name': NAMES[i], 'close': 100.0 + pct,
                    'pct_chg': pct, 'volume': 0.0, 'amount': 0.0,
                    'extra': '{}'})
    return out


P15 = [round(1.00 - 0.05 * i, 2) for i in range(N)]
P11 = [round(1.05 - 0.05 * i, 2) for i in range(N)]
P09 = [0.0] * N

conn = db.get_connection(TMP_DB)
db.upsert_snapshot_rows(conn, _rows(B, '09:25', P09)
                        + _rows(B, '11:30', P11)
                        + _rows(B, '15:00', P15))
conn.commit()
print('  依据日 %s：写入 %d 板块 × 3 时点 = %d 行'
      % (B, N, 3 * N))

sec('D2) board_candidates 只取最佳时点单份快照（不得混入 09:25/11:30）')
cand = pr.board_candidates(conn, B)
ok('返回非空候选', bool(cand))
ok('slot = 最佳时点 15:00', cand.get('slot') == '15:00', cand.get('slot'))
ok('候选行数 = 单时点板块数 %d（原缺陷为 3 倍 %d）'
   % (N, 3 * N), len(cand['boards']) == N, len(cand['boards']))
ok('候选无同名重复行', len({b['name'] for b in cand['boards']}) == N)
_t18 = cand['tops'][:18]
ok('涨幅榜前 18 全为不同板块（原缺陷按三时点混排只有 9 个）',
   len({b['name'] for b in _t18}) == 18, len({b['name'] for b in _t18}))
ok('榜首 = N00 取 15:00 收盘 1.00（原缺陷取 11:30 的 1.05）',
   _t18[0]['name'] == 'N00' and _t18[0]['pct_chg'] == 1.00
   and _t18[0]['slot'] == '15:00',
   (_t18[0]['name'], _t18[0]['pct_chg'], _t18[0]['slot']))
ok('跌幅榜末位 = N19（15:00 值 0.05；原缺陷会取到 09:25 的 0.0）',
   cand['bottoms'][0]['name'] == 'N19'
   and cand['bottoms'][0]['pct_chg'] == 0.05,
   (cand['bottoms'][0]['name'], cand['bottoms'][0]['pct_chg']))
ok('候选自带 slot 字段便于溯源', all('slot' in b for b in cand['boards']))
conn.close()

sec('D1) 自动调参覆盖在 LLM 路径上真正生效（effective_params 单一入口）')
_res = pr.apply_param_changes({'generation.n_leading': 5,
                               'generation.n_risk': 4,
                               'generation.temperature': 0.7,
                               'reflect.window_days': 7},
                              reason='regression', source='manual')
_g = pr._run_params(CFG, 'generation')
ok('覆盖写入 predict_params 且被 effective_params 读到（5/4/0.7）',
   _g.get('n_leading') == 5 and _g.get('n_risk') == 4
   and _g.get('temperature') == 0.7,
   json.dumps({'changed': _res.get('changed')}, ensure_ascii=False))

CAP = {}


def _stub_chat(system_prompt, user_message, temperature=None,
               max_tokens=None, stream=False, on_delta=None, timeout=None):
    """打桩模型：刻意只给 2 领涨 + 1 风险（少于 n_leading/n_risk）。

    签名必须与 model_interface.chat_completion 保持同步：predict.py 会按
    cfg['model']['timeout_seconds'] 透传 timeout，签名漂移会让打桩失败并
    静默回退规则基线，导致 D1 段断言失真。
    """
    CAP['user'] = user_message
    CAP['temperature'] = temperature
    CAP['max_tokens'] = max_tokens
    CAP['timeout'] = timeout
    return json.dumps({'stance': '回归测试基调',
                       'leading': [{'board': 'N00', 'thesis': '模型选定 A'},
                                   {'board': 'N01', 'thesis': '模型选定 B'}],
                       'risk': [{'board': 'N19', 'thesis': '模型选定 C'}]},
                      ensure_ascii=False)


mi.chat_completion = _stub_chat

g = pr.generate_day(CFG, date=D1, basis_date=B)
ok('generate action=generated', g.get('action') == 'generated',
   json.dumps({k: v for k, v in g.items() if k != 'themes'},
              ensure_ascii=False))
ok('basis_slot 透出 15:00（生成口径可溯源）',
   g.get('basis_slot') == '15:00', g.get('basis_slot'))
ok('prompt 按生效参数出题「选 5 个领涨 / 选 4 个风险」'
   '（原缺陷按基线出 3/2）',
   '从涨幅榜候选中选 5 个' in CAP['user']
   and '从跌幅榜候选中选 4 个' in CAP['user'],
   [ln for ln in CAP['user'].split('\n') if '从' in ln and '选 ' in ln])
ok('temperature 用生效值 0.7（原缺陷用基线 0.4）',
   CAP['temperature'] == 0.7, CAP['temperature'])
ok('model.timeout_seconds 真正透传到 chat_completion'
   '（原缺陷：该配置键无任何消费点，代码硬编码 120，配置 150 为“谎报”）',
   CAP['timeout'] == (CFG.get('model') or {}).get('timeout_seconds'),
   CAP['timeout'])
ok('tunable_bounds 不含无运行时读取点的 dify 键'
   '（原缺陷：dify.retrieval_top_k 会被自动调参应用，改写指纹却零行为影响）',
   pr._bounded({'dify.retrieval_top_k': 12, 'dify.max_tokens': 512,
                'dify.temperature': 0.9},
               CFG.get('tunable_bounds') or {}) == {},
   sorted(CFG.get('tunable_bounds') or {}))
ok('产出条数补足到 n_leading/n_risk（原缺陷恒为模型给的 2/1）',
   g.get('count_leading') == 5 and g.get('count_risk') == 4,
   (g.get('count_leading'), g.get('count_risk')))

conn = db.get_connection(TMP_DB)
_rows_db = conn.execute('SELECT side, seq, theme, rationale, model, params_fp '
                        'FROM predictions WHERE trade_date=? '
                        'ORDER BY side, seq', (D1,)).fetchall()
conn.close()
_lead = [r for r in _rows_db if r['side'] == 'leading']
_risk = [r for r in _rows_db if r['side'] == 'risk']
ok('predictions 落库 5 领涨 + 4 风险',
   len(_lead) == 5 and len(_risk) == 4, (len(_lead), len(_risk)))
ok('模型自身选中的 2 条排在 seq 1-2，未被补足顶替',
   [r['theme'] for r in _lead[:2]] == ['N00', 'N01'],
   [r['theme'] for r in _lead])
ok('补足条目带可追溯标记「候选补足」',
   all('候选补足' in r['rationale'] for r in _lead[2:])
   and all('候选补足' in r['rationale'] for r in _risk[1:]),
   [r['rationale'][:20] for r in _lead])
ok('补足未与模型所选（含跨侧）重复',
   len({r['theme'] for r in _rows_db}) == len(_rows_db),
   [r['theme'] for r in _rows_db])
ok('全行 model=deepseek（补足不改变模型口径标记）',
   all(r['model'] == 'deepseek' for r in _rows_db))
_fp = pr.params_fp(pr.effective_params(CFG))
ok('入库指纹 = 生效参数指纹（模型看到的口径与指纹同源）',
   g.get('params_fp') == _fp
   and all(r['params_fp'] == _fp for r in _rows_db),
   (g.get('params_fp'), _fp))
ok('日志记录候选补足动作',
   any('候选补足' in m for m in _LOGGED),
   [m for m in _LOGGED if '补足' in m or '[生成]' in m][-2:])

sec('D1b) 同类读取点（reflect / predict_baseline）同样走生效参数')
ok('reflect 段读生效值 window_days=7（原读基线 20）',
   pr._run_params(CFG, 'reflect').get('window_days') == 7,
   pr._run_params(CFG, 'reflect').get('window_days'))
ok('predict_baseline 生成口径读生效值 (5, 4)（原读基线 (3, 2)）',
   pb.gen_params(CFG) == (5, 4), pb.gen_params(CFG))
pr.apply_param_changes({'settle.leading_hit_rank': 9},
                       reason='regression', source='manual')
ok('predict_baseline 结算口径读生效值 leading_hit_rank=9（原读基线 12）',
   pb.settle_params(CFG)[0] == 9, pb.settle_params(CFG))

sec('D1c) 模型完全不可用时仍回退规则基线（不引入回归）')
D2D = '2026-08-19'


def _boom(*a, **k):
    raise RuntimeError('模拟模型不可用')


mi.chat_completion = _boom
g2 = pr.generate_day(CFG, date=D2D, basis_date=B)
conn = db.get_connection(TMP_DB)
_rows2 = conn.execute('SELECT side, theme, model FROM predictions '
                      'WHERE trade_date=?', (D2D,)).fetchall()
conn.close()
ok('回退规则基线 model=rule',
   g2.get('model') == 'rule' and all(r['model'] == 'rule' for r in _rows2),
   g2.get('model'))
ok('规则基线同样按生效参数出 5 领涨 + 4 风险（原会按基线出 3/2）',
   g2.get('count_leading') == 5 and g2.get('count_risk') == 4,
   (g2.get('count_leading'), g2.get('count_risk')))
ok('规则基线取涨幅榜前 5 / 跌幅榜前 4',
   sorted(r['theme'] for r in _rows2 if r['side'] == 'leading')
   == ['N00', 'N01', 'N02', 'N03', 'N04']
   and sorted(r['theme'] for r in _rows2 if r['side'] == 'risk')
   == ['N16', 'N17', 'N18', 'N19'],
   sorted(r['theme'] for r in _rows2 if r['side'] == 'risk'))

sec('D3) 非交易日不落库（周末重盖章行 → 基线评估同义反复满分）')
import market_snapshot as ms  # noqa: E402

_TODAY = ms.now_local(CFG).strftime('%Y-%m-%d')
_ms_state_path, _ms_is_td = ms.STATE_PATH, ms.is_trade_day
_ms_state = TMP_ROOT / 'market_state.json'
ms.STATE_PATH = _ms_state

_mk_cfg = copy.deepcopy(_base)
_mk_cfg['snapshot_points'] = [{'time': '00:00', 'label': 'test',
                               'allow_late_minutes': 1440}]
_st_before = {'date': _TODAY, 'slots': {}}
ms.is_trade_day = lambda d: False          # 模拟周末/节假日
_act = ms.tick(_mk_cfg, state=copy.deepcopy(_st_before))
conn = db.get_connection(TMP_DB)
_d3n = conn.execute('SELECT COUNT(*) AS c FROM market_snapshot '
                    'WHERE trade_date=?', (_TODAY,)).fetchone()['c']
conn.close()
ok('非交易日 tick 返回 skipped(non_trading_day)',
   [a.get('action') for a in _act] == ['skipped']
   and _act[0].get('detail') == 'non_trading_day',
   _act)
ok('非交易日不向 market_snapshot 写任何行（原缺陷写 230 行/时点）', _d3n == 0,
   _d3n)
ok('非交易日状态置终态 skipped，避免每 30 秒重试',
   (json.loads(_ms_state.read_text(encoding='utf-8'))['slots']
    .get(_mk_cfg['snapshot_points'][0]['time'], {}).get('status')) == 'skipped')

_rs, _td = ms.run_snapshot, ms.is_trade_day
ms.run_snapshot = lambda conn, cfg, slot, trade_date=None: {'total': 0,
                                                            'by_kind': {}}
ms.is_trade_day = lambda d: True
_act2 = ms.tick(_mk_cfg, state={'date': _TODAY, 'slots': {}})
ms.run_snapshot, ms.is_trade_day = _rs, _td
ms.STATE_PATH = _ms_state_path           # 还原，避免污染后续用例
ok('交易日仍会走到抓取（守卫不误拦）',
   [a.get('action') for a in _act2] == ['skipped']
   and _act2[0].get('detail') is None, _act2)

sec('D4) predict_baseline.recent_dates 以交易日历过滤 market_snapshot')
conn = db.get_connection(TMP_DB)
_WK = ['2026-08-13', '2026-08-14', '2026-08-15', '2026-08-16']  # 四/五/六/日
db.upsert_snapshot_rows(conn, [r for _d in _WK
                               for r in _rows(_d, '15:00', [1.0, 1.0])])
conn.commit()
_old = [r['trade_date'] for r in conn.execute(
    "SELECT DISTINCT trade_date FROM market_snapshot WHERE slot='15:00' "
    "ORDER BY trade_date DESC LIMIT 3").fetchall()][::-1]
ms.is_trade_day = lambda d: d not in ('2026-08-15', '2026-08-16')
_nb = pb.recent_dates(conn, 3)
ms.is_trade_day = _ms_is_td
conn.close()
ok('原缺陷口径确会取到周末 08-15/08-16（回归前提成立）',
   _old == ['2026-08-15', '2026-08-16', '2026-08-17'], _old)
ok('修复后只取真实交易日（跳过 08-15/08-16）',
   '2026-08-15' not in _nb and '2026-08-16' not in _nb, _nb)
ok('修复后结果升序且仍取满 3 天',
   _nb == sorted(_nb) and len(_nb) == 3, _nb)

sec('D5) 整日漏生成/漏结算必须落 predict_days 台账（防静默丢失）')
_FAKE = ['2026-08-19', '2026-08-20', '2026-08-24']
_tdb = ms.trading_days_between
ms.trading_days_between = lambda s, e: [d for d in _FAKE if s <= d <= e]
_pn = pr.now_local
pr.now_local = lambda cfg=None: datetime(2026, 8, 24, 23, 0)
Path(pr.STATE_PATH).write_text(json.dumps(
    {'date': '2026-08-19', 'morning': {'decided': True}}), encoding='utf-8')
_act3 = pr.tick(CFG, date='2026-08-24')
conn = db.get_connection(TMP_DB)
_d20 = conn.execute('SELECT status, summary FROM predict_days WHERE '
                    'trade_date=?', ('2026-08-20',)).fetchone()
_d24 = conn.execute('SELECT status, summary FROM predict_days WHERE '
                    'trade_date=?', ('2026-08-24',)).fetchone()
_d19 = conn.execute('SELECT status FROM predict_days WHERE trade_date=?',
                    ('2026-08-19',)).fetchone()
conn.close()
ms.trading_days_between = _tdb
ok('tick 超兜底返回 missed 且写库', [a.get('action') for a in _act3] == ['missed']
   and '已落库' in _act3[0].get('detail', ''), _act3)
ok('当日零生成 → predict_days 落 missed 台账行（原缺陷库中无任何痕迹）',
   bool(_d24) and _d24['status'] == 'missed',
   dict(_d24) if _d24 else None)
ok('missed 行带可审计原因 over_deadline',
   bool(_d24) and json.loads(_d24['summary']).get('missed_reason',
                                                  '').startswith('over_deadline'),
   json.loads(_d24['summary']) if _d24 else None)
ok('区间内「零生成」的历史交易日由 backfill 补台账（原实现 UPDATE 0 行）',
   bool(_d20) and _d20['status'] == 'missed'
   and json.loads(_d20['summary']).get('missed_reason') == 'backfill',
   json.loads(_d20['summary']) if _d20 else None)
ok('已有预测的交易日不被误标 missed（08-19 保持原状态）',
   bool(_d19) and _d19['status'] != 'missed',
   dict(_d19) if _d19 else None)
_a4 = pr.tick(CFG, date='2026-08-24')
pr.now_local = _pn                        # 还原时钟，避免影响后续用例
ok('重复 tick 幂等（已决策不再写库、不重复补行）', _a4 == [], _a4)

print()
if FAIL:
    print('FAILED (%d): %s' % (len(FAIL), ', '.join(FAIL)))
    print('临时库保留于: ' + str(TMP_ROOT))
    sys.exit(1)
if os.environ.get('REQ3_REG_KEEP'):
    print('临时库保留(REQ3_REG_KEEP)于: ' + str(TMP_ROOT))
else:
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
print('ALL PASS')
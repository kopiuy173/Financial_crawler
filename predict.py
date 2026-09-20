#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""predict.py — 需求 3：盘前主题预测 → 收盘复盘（vs_outcome 回填）→ LLM 反思闭环"""
from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo  # noqa: F401  时间口径统一走 market_snapshot
except ImportError:  # Python < 3.9
    ZoneInfo = None

_PROJ = Path(__file__).resolve().parent
os.chdir(_PROJ)

import yaml  # noqa: E402
import database as db  # noqa: E402
import market_snapshot as ms  # noqa: E402
import model_interface as mi  # noqa: E402 （DeepSeek；Dify 模型接线后同入口扩展）

CONFIG_PATH = Path(os.environ.get("PREDICT_CONFIG",
                                  _PROJ / "config" / "predict.yaml"))
LOG_PATH = Path(os.environ.get("PREDICT_LOG",
                                _PROJ / "logs" / "predict.log"))
STATE_PATH = Path(os.environ.get("PREDICT_STATE",
                                  _PROJ / "logs" / "predict_state.json"))

_MORN_TERMINAL = ("done", "late", "missed", "skipped")


def now_local(cfg=None):
    return ms.now_local(cfg if cfg else {})


def today_str(cfg=None):
    return now_local(cfg if cfg else {}).strftime("%Y-%m-%d")


def log(msg):
    line = f"[{now_local():%F %T}] {msg}"
    LOG_PATH.parent.mkdir(exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def load_config(path=CONFIG_PATH):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def get_db_path():
    return os.environ.get("KB_DB_PATH") or db.get_db_path()


def get_conn():
    return db.get_connection(get_db_path())


def ensure_schema():
    db.ensure_market_schema(get_db_path())


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"date": "", "morning": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(STATE_PATH)


def _hhmm(s):
    hh, mm = [int(x) for x in str(s or "00:00").split(":")[:2]]
    return hh * 60 + mm

# 指纹 = 生效 params 的 sha256 前 16 位（历史表承载回滚链）
def _deep(v):
    return copy.deepcopy(v)


def _paths(d, prefix=""):
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            yield from _paths(v, p)
        else:
            yield p


def _path_get(d, path, default=None):
    cur = d
    for k in str(path).split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _path_set(d, path, value):
    keys = str(path).split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value
    return d


def _norm(v):
    """规范化：数字统一转 float，保证 3 与 3.0 指纹一致。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def canonical_json(v):
    return json.dumps(_norm(v), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def params_fp(params):
    """参数指纹：sha256(规范化 json) 前 16 位。"""
    return hashlib.sha256(canonical_json(params).encode("utf-8")).hexdigest()[:16]


def _overrides():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM predict_params").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}
    finally:
        conn.close()


def effective_params(cfg):
    """生效参数 = yaml 基线 + 库内 overrides 深度合并。"""
    base = _deep((cfg.get("params") or {}))
    for path, value in _overrides().items():
        _path_set(base, path, value)
    return base


def _run_params(cfg, section):
    """运行时读取某段**生效**参数（yaml 基线 + predict_params 覆盖）。

    铁律：任何影响行为/口径的 params 读取都必须走这里，绝不能直接读
    cfg['params']——否则自动调参（或人工 apply/rollback）写进 predict_params
    的覆盖值会被静默忽略，出现「指纹已变、行为未变」的假调参。
    历史缺陷实例：_model_pick 曾读 cfg['params']，使 generation.n_leading /
    n_risk / temperature 的覆盖在 LLM 路径上全部失效。
    """
    return (effective_params(cfg).get(section) or {})


def _write_overrides(mapping):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM predict_params")
        ts = _now_iso()
        for path in sorted(mapping):
            conn.execute(
                "INSERT OR REPLACE INTO predict_params (key, value, updated_at) "
                "VALUES (?,?,?)",
                (path, json.dumps(mapping[path], ensure_ascii=False), ts))
        conn.commit()
    finally:
        conn.close()


def _push_history(params, reason, source, parent_fp=""):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO predict_param_history "
            "(fp, parent_fp, params, reason, source, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (params_fp(params), parent_fp,
             json.dumps(params, ensure_ascii=False), reason, source, _now_iso()))
        conn.commit()
    finally:
        conn.close()


def apply_param_changes(changes, reason="manual", source="manual"):
    """按点路径批量改动 params；先存“改前”历史（含父指纹回滚链）再写覆盖。"""
    changes = {k: v for k, v in (changes or {}).items()}
    if not changes:
        return {"old_fp": params_fp(effective_params(load_config())),
                "new_fp": params_fp(effective_params(load_config())),
                "changed": []}
    cfg = load_config()
    before = effective_params(cfg)
    after = _deep(before)
    for path, value in changes.items():
        _path_set(after, path, value)
    _push_history(before, reason=f"{reason}（apply 前）", source=source)
    mapping = {}
    base = cfg.get("params") or {}
    for p in _paths(after):
        v = _path_get(after, p)
        if not _path_get(base, p, None) == v or p in changes:
            mapping[p] = v
    _write_overrides(mapping)
    new_fp = params_fp(after)
    _push_history(after, reason=f"{reason}（apply 后）", source=source,
                  parent_fp=params_fp(before))
    return {"old_fp": params_fp(before), "new_fp": new_fp,
            "changed": sorted(mapping)}


def rollback_to(fp_hint, reason="rollback"):
    """回滚到指定指纹（支持前几位前缀）；写入目标态为新的当前态。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT params FROM predict_param_history WHERE fp LIKE ? "
            "ORDER BY id", (f"{fp_hint}%",)).fetchall()
        if not rows:
            raise ValueError(f"predict_param_history 中找不到指纹前缀 {fp_hint!r}")
        target = json.loads(rows[-1]["params"])
    finally:
        conn.close()
    cfg = load_config()
    before = effective_params(cfg)
    base = cfg.get("params") or {}
    mapping = {}
    for p in _paths(target):
        v = _path_get(target, p)
        if not _path_get(base, p, None) == v:
            mapping[p] = v
    _write_overrides(mapping)
    _push_history(target, reason=reason, source="rollback",
                  parent_fp=params_fp(before))
    return {"old_fp": params_fp(before), "new_fp": params_fp(target),
            "changed": sorted(mapping)}

_SLOT_PRIORITY = ["15:00", "11:30", "09:25"]


def _parse_extra(raw):
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def snapshot_boards(conn, trade_date, slot=None):
    """读取某交易日板块快照行（含 slot/extra/领涨股），可选指定时点。"""
    if slot:
        rows = conn.execute(
            "SELECT slot, kind, code, name, pct_chg, amount, extra "
            "FROM market_snapshot WHERE trade_date=? AND slot=? "
            "AND kind IN ('industry_board','concept_board') AND pct_chg IS NOT NULL",
            (trade_date, slot)).fetchall()
    else:
        rows = conn.execute(
            "SELECT slot, kind, code, name, pct_chg, amount, extra "
            "FROM market_snapshot WHERE trade_date=? "
            "AND kind IN ('industry_board','concept_board') AND pct_chg IS NOT NULL",
            (trade_date,)).fetchall()
    out = []
    for r in rows:
        extra = _parse_extra(r["extra"])
        out.append({
            "slot": r["slot"], "kind": r["kind"], "code": r["code"],
            "name": r["name"], "pct_chg": float(r["pct_chg"]),
            "amount": r["amount"], "leader": extra.get("leader") or "",
            "leader_pct": extra.get("leader_pct"),
            "api": extra.get("api") or "",
        })
    return out


def _best_slot(conn, trade_date):
    """按 15:00 > 11:30 > 09:25 选有数据时点。"""
    got = {r["slot"] for r in conn.execute(
        "SELECT DISTINCT slot FROM market_snapshot WHERE trade_date=? "
        "AND kind IN ('industry_board','concept_board')", (trade_date,)).fetchall()}
    for s in _SLOT_PRIORITY:
        if s in got:
            return s
    return None


def resolve_basis_date(conn, target_date):
    """找 target_date 之前最近、且有可用板块快照的交易日（回退最多 14 自然日）。

    判定口径与 board_candidates 对齐：以该交易日**最佳时点**的非空 pct_chg
    行数为准，避免选中「只有盘前 09:25 全 0 快照」的日子当依据日。
    """
    t = datetime.strptime(target_date, "%Y-%m-%d")
    start = (t - timedelta(days=14)).strftime("%Y-%m-%d")
    end = (t - timedelta(days=1)).strftime("%Y-%m-%d")
    for d in reversed(ms.trading_days_between(start, end) or []):
        slot = _best_slot(conn, d)
        if not slot:
            continue
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM market_snapshot WHERE trade_date=? AND slot=? "
            "AND kind IN ('industry_board','concept_board') "
            "AND pct_chg IS NOT NULL", (d, slot)).fetchone()["c"]
        if n >= 10:
            return d
    return None


def board_candidates(conn, basis_date):
    """前一交易日板块映射 → 涨幅榜/跌幅榜候选 + 名称→板块索引。

    口径（USER_GUIDE D3「口径与信息集一致」）：只取该交易日**最佳时点的单份**
    快照（正常为 15:00 收盘），与 settle / predict_baseline 的评估口径一致。
    历史缺陷：此处曾不传 slot（等价于把 09:25 盘前全 0、11:30 盘中、15:00 收盘
    三份快照混在一起排序），导致同一板块以不同时点的涨跌幅重复出现在候选榜里
    （例如 top18 实际只有 11 个不同板块），模型被喂到的并非收盘涨幅榜。
    """
    slot = _best_slot(conn, basis_date)
    boards = snapshot_boards(conn, basis_date, slot) if slot else []
    if len(boards) < 10:
        return None
    name_map, uniq = {}, []
    for b in boards:
        if b["name"] in name_map:
            continue
        name_map[b["name"]] = b
        uniq.append(b)
    if len(uniq) != len(boards):
        log("  [warn] 依据日 " + str(basis_date) + " 候选板块存在同名重复行（"
            + str(len(boards) - len(uniq)) + " 条），已按首次出现去重")
    return {
        "basis_date": basis_date, "slot": slot, "boards": uniq,
        "tops": sorted(uniq, key=lambda r: r["pct_chg"], reverse=True),
        "bottoms": sorted(uniq, key=lambda r: r["pct_chg"]),
        "name_map": name_map,
    }


def _kind_cn(kind):
    return "行业" if kind == "industry_board" else "概念"


def _fmt_cands(list_, limit):
    lines = []
    for i, b in enumerate(list_[:limit], 1):
        lead = f" 领涨:{b['leader']}" if b.get("leader") else ""
        lines.append(f"{i}. [{_kind_cn(b['kind'])}]{b['name']} "
                     f"{b['pct_chg']:+.2f}%{lead}")
    return "\n".join(lines)


def _news_digest(conn, basis_date, days=2, limit=40):
    """依据日（含前 days-1 天）新闻标题摘要，仅供模型参考。"""
    if not basis_date or int(days or 0) < 1:
        return []
    t = datetime.strptime(basis_date, "%Y-%m-%d")
    start = (t - timedelta(days=int(days) - 1)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT publish_date, title FROM documents "
        "WHERE source_type='news' AND publish_date BETWEEN ? AND ? "
        "ORDER BY publish_date DESC, id DESC LIMIT ?",
        (start, basis_date, int(limit))).fetchall()
    return [f"[{r['publish_date']}] {r['title']}" for r in rows]

def _extract_json(text):
    if not text or not str(text).strip():
        raise ValueError('模型返回空文本')
    s = str(text).strip()
    if s.startswith('```'):
        s = s.split('```', 2)[1].strip()
        if s.startswith('json'):
            s = s[4:].strip()
    a, z = s.find('{'), s.rfind('}')
    if a < 0 or z <= a:
        raise ValueError('模型响应中未找到 JSON 对象')
    return json.loads(s[a:z + 1])


def _resolve_name(name, cand):
    nm = str(name or '').strip()
    if nm in cand['name_map']:
        return nm, cand['name_map'][nm]
    hits = difflib.get_close_matches(nm, list(cand['name_map']), n=1, cutoff=0.82)
    if hits:
        return hits[0], cand['name_map'][hits[0]]
    return None, None


def _rule_themes(cand, n_leading, n_risk):
    """规则基线：直接取昨日板块榜顶/榜尾，绝不依赖外部接口。"""
    out = []
    for i, b in enumerate(cand['tops'], 1):
        if len([t for t in out if t['side'] == 'leading']) >= n_leading:
            break
        out.append({'side': 'leading', 'name': b['name'], 'kind': b['kind'],
                    'code': b['code'],
                    'thesis': '规则基线：依据日[' + _kind_cn(b['kind']) + ']涨幅榜第 '
                              + str(i) + '（' + ('%+.2f%%' % b['pct_chg'])
                              + '），动量延续候选。'})
    for i, b in enumerate(cand['bottoms'], 1):
        if len([t for t in out if t['side'] == 'risk']) >= n_risk:
            break
        out.append({'side': 'risk', 'name': b['name'], 'kind': b['kind'],
                    'code': b['code'],
                    'thesis': '规则基线：依据日[' + _kind_cn(b['kind']) + ']跌幅榜第 '
                              + str(i) + '（' + ('%+.2f%%' % b['pct_chg'])
                              + '），弱势回避候选。'})
    return out


def _pad_side(themes, side, want, cand, key, label):
    """模型漏选时按候选榜顺序补足到 want 条；返回补足条数（确定性、跨侧去重）。

    LLM 不保证严格给出 n_leading / n_risk 条（实测要求 5 条只给 3 条），
    仅靠 [:want] 只能截断、不能补足，会让 generation.n_leading / n_risk 形同虚设。
    """
    have = {t['name'] for t in themes}
    n = 0
    for i, b in enumerate(cand[key], 1):
        if len([t for t in themes if t['side'] == side]) >= want:
            break
        if b['name'] in have:
            continue
        have.add(b['name'])
        themes.append({
            'side': side, 'name': b['name'], 'kind': b['kind'],
            'code': b['code'],
            'thesis': '候选补足（模型条数不足）：依据日['
                      + _kind_cn(b['kind']) + ']' + label + '第 ' + str(i)
                      + '（' + ('%+.2f%%' % b['pct_chg']) + '）。'})
        n += 1
    return n


def _model_pick(cfg, cand, digest, params=None):
    """调 DeepSeek 从候选榜里选 领涨/风险 主题；board 名必须逐字取自候选。

    params 必须与 generate_day 用于算指纹的**同一份** effective_params，
    保证「模型看到的口径」与「入库指纹」一致（否则调参会变成假调参）。
    """
    params = effective_params(cfg) if params is None else params
    g = params.get('generation', {})
    top = int(g.get('context_top') or 18)
    bottom = int(g.get('context_bottom') or 12)
    nl = int(g.get('n_leading') or 3)
    nr = int(g.get('n_risk') or 2)
    digest_text = chr(10).join(digest) if digest else '(无)'
    lines = []
    lines.append('【依据交易日】' + cand['basis_date'] + '（前一交易日）板块映射。')
    lines.append('【昨日板块涨幅榜候选（前 ' + str(top) + '）】')
    lines.append(_fmt_cands(cand['tops'], top))
    lines.append('【昨日板块跌幅榜候选（前 ' + str(bottom) + '）】')
    lines.append(_fmt_cands(cand['bottoms'], bottom))
    lines.append('【近两日新闻标题摘要（仅供参考）】')
    lines.append(digest_text)
    lines.append('')
    lines.append('请输出：')
    lines.append('1) stance：40 字内一句话研判当日（依据日次日）整体基调；')
    lines.append('2) leading：从涨幅榜候选中选 ' + str(nl)
                 + ' 个当日最可能继续领涨的主题；')
    lines.append('3) risk：从跌幅榜候选中选 ' + str(nr) + ' 个建议回避/看空的主题；')
    lines.append('每条 thesis 不超过 60 字，尽量引用依据日实际涨跌幅数字。')
    lines.append('只输出一个 JSON 对象，不要 markdown 代码块。字段结构：')
    lines.append('stance 为字符串；leading 与 risk 为数组；数组元素为对象，'
                 '含 board（主题名，必须逐字与候选列表一致）与 thesis（理由）。')
    system = ('你是A股主题投资的盘前研究员。基于前一交易日板块涨幅/跌幅候选榜，'
              '为次日挑选最可能领涨与建议规避的主题。铁律：board 名称必须逐字取自'
              '候选列表，严禁编造列表之外的板块；理由须引用依据日涨跌幅数字；'
              '输出克制中性，不渲染情绪。')
    m = cfg.get('model', {})
    txt = mi.chat_completion(system, chr(10).join(lines),
                             temperature=float(g.get('temperature') or 0.4),
                             max_tokens=int(g.get('max_tokens') or 1400),
                             timeout=m.get('timeout_seconds'))
    data = _extract_json(txt)
    themes = []
    for side, arr, want in (('leading', data.get('leading'), nl),
                            ('risk', data.get('risk'), nr)):
        for it in (arr or [])[: want]:
            nm, b = _resolve_name((it or {}).get('board'), cand)
            if b:
                themes.append({'side': side, 'name': b['name'],
                               'kind': b['kind'], 'code': b['code'],
                               'thesis': str((it or {}).get('thesis') or '').strip()})
    if not themes:
        raise ValueError('模型未返回任何可用主题，改走规则基线')
    pad_l = _pad_side(themes, 'leading', nl, cand, 'tops', '涨幅榜')
    pad_r = _pad_side(themes, 'risk', nr, cand, 'bottoms', '跌幅榜')
    if pad_l or pad_r:
        log('  [候选补足] 模型条数不足：领涨补 ' + str(pad_l) + ' / 风险补 '
            + str(pad_r) + '（目标 n_leading=' + str(nl)
            + ' n_risk=' + str(nr) + '）')
    have = {t['side'] for t in themes}
    if have != {'leading', 'risk'}:
        raise ValueError('模型返回主题两侧不完整且候选不足，改走规则基线')
    return themes, str(data.get('stance') or '').strip()


def _insert_pred_rows(conn, trade_date, rows, fp, basis_date='', model='rule',
                      params_snapshot=None):
    """按 (trade_date, side, seq) 幂等写 predictions；返回 (领涨数, 风险数)。"""
    ts = _now_iso()
    snap = json.dumps(params_snapshot or {}, ensure_ascii=False)
    counters = {}
    nl_, nr_ = 0, 0
    for r in rows:
        side = r['side']
        counters[side] = counters.get(side, 0) + 1
        if side == 'leading':
            nl_ += 1
        elif side == 'risk':
            nr_ += 1
        conn.execute(
            'INSERT OR REPLACE INTO predictions '
            '(trade_date, basis_date, side, seq, theme, kind, board_code, '
            'rationale, status, params_fp, params_snapshot, model, '
            'created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (trade_date, basis_date, side, counters[side], r['theme'],
             r['kind'], r['board_code'], r['rationale'], 'pending', fp, snap,
             model, ts, ts))
    return nl_, nr_


def _set_day(conn, trade_date, fields):
    """upsert predict_days 单日行（trade_date 唯一；显式先查再写，规避 UPSERT 的 NOT NULL 先验问题）。"""
    fields = dict(fields)
    ts = _now_iso()
    row = conn.execute('SELECT id FROM predict_days WHERE trade_date=?',
                       (trade_date,)).fetchone()
    if row:
        assigns = ','.join(k + '=?' for k in fields)
        conn.execute('UPDATE predict_days SET ' + assigns
                     + ' WHERE trade_date=?',
                     [fields[k] for k in fields] + [trade_date])
        return
    f2 = dict(fields)
    f2.setdefault('created_at', ts)
    f2.setdefault('updated_at', ts)
    cols = ['trade_date'] + list(f2)
    ph = ','.join('?' * len(cols))
    conn.execute('INSERT INTO predict_days (' + ','.join(cols) + ') VALUES ('
                 + ph + ')',
                 [trade_date] + [f2[k] for k in f2])


def generate_day(cfg=None, date=None, basis_date=None, force=False, no_llm=False):
    """盘前生成当日预测；幂等（重复调用跳过 / force 且无已结算时才重建）。"""
    cfg = cfg or load_config()
    if not cfg.get('enabled'):
        return {'action': 'skipped', 'detail': ' predict.yaml enabled=false'}
    date = date or today_str(cfg)
    conn = get_conn()
    try:
        rows0 = conn.execute('SELECT status FROM predictions WHERE trade_date=?',
                             (date,)).fetchall()
        evaled = sum(1 for r in rows0 if r['status'] == 'evaluated')
        if rows0:
            if evaled:
                return {'action': 'refused',
                        'detail': ' ' + date + ' 已有 ' + str(evaled)
                                  + ' 条已结算预测，拒绝覆盖'}
            if not force:
                return {'action': 'existing',
                        'detail': ' ' + date + ' 已有 ' + str(len(rows0))
                                  + ' 条 pending 预测，幂等跳过'}
            conn.execute('DELETE FROM predictions WHERE trade_date=?', (date,))

        params = effective_params(cfg)
        fp = params_fp(params)
        basis = basis_date or resolve_basis_date(conn, date)
        if not basis:
            return {'action': 'no_basis',
                    'detail': ' ' + date + ' 之前 14 日内找不到带板块快照的交易日'}
        cand = board_candidates(conn, basis)
        if not cand:
            return {'action': 'no_basis', 'detail': ' 依据日 ' + basis + ' 板块行不足'}

        g = params.get('generation', {})
        digest = _news_digest(conn, basis,
                              days=int(g.get('news_digest_days') or 2),
                              limit=int(g.get('news_digest_limit') or 40))
        model_used, stance, themes = 'none', '', []
        if cfg.get('model', {}).get('enabled') and not no_llm:
            try:
                themes, stance = _model_pick(cfg, cand, digest, params)
                model_used = str(cfg.get('model', {}).get('backend') or 'deepseek')
            except Exception as e:  # noqa: BLE001 —— 模型失败绝不断流水线
                log('  LLM 生成失败，回退规则基线: ' + type(e).__name__ + ': ' + str(e))
        if not themes:
            themes = _rule_themes(cand, int(g.get('n_leading') or 3),
                                  int(g.get('n_risk') or 2))
            model_used = 'rule'

        rows_db = [{'side': t['side'], 'theme': t['name'], 'kind': t['kind'],
                    'board_code': t['code'],
                    'rationale': t.get('thesis') or ''} for t in themes]
        nl_, nr_ = _insert_pred_rows(conn, date, rows_db, fp,
                                     basis_date=basis, model=model_used,
                                     params_snapshot=params)
        summary = json.dumps({'stance': stance, 'basis_date': basis,
                              'count_leading': nl_, 'count_risk': nr_,
                              'model': model_used}, ensure_ascii=False)
        ts = _now_iso()
        _set_day(conn, date, {'basis_date': basis, 'generated_at': ts,
                              'model': model_used, 'params_fp': fp,
                              'params_snapshot': json.dumps(params, ensure_ascii=False),
                              'summary': summary, 'status': 'generated',
                              'created_at': ts, 'updated_at': ts})
        conn.commit()
        log('  [生成] ' + date + ' basis=' + basis
            + ' slot=' + str(cand.get('slot') or '') + ' model=' + model_used
            + ' leading=' + str(nl_) + ' risk=' + str(nr_) + ' fp=' + fp)
        return {'action': 'generated', 'date': date, 'basis_date': basis,
                'basis_slot': cand.get('slot'), 'model': model_used,
                'count_leading': nl_, 'count_risk': nr_,
                'params_fp': fp,
                'themes': [{'side': t['side'], 'name': t['name']}
                           for t in themes]}
    finally:
        conn.close()


def _parse_day_summary(raw):
    try:
        v = json.loads(raw or '{}')
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def _board_index(boards):
    """按板块类型建 涨幅榜名称序列 + 名称→涨跌幅 索引。"""
    groups = {}
    for b in boards:
        groups.setdefault(b['kind'], []).append(b)
    idx = {}
    for kind, arr in groups.items():
        desc = sorted(arr, key=lambda x: x['pct_chg'], reverse=True)
        idx[kind] = {'names': [b['name'] for b in desc],
                     'pct': {b['name']: b['pct_chg'] for b in desc},
                     'n': len(desc)}
    return idx


def settle(cfg=None, date=None):
    """收盘结算：以当日（15:00 优先）板块整表回填 predictions.vs_outcome。"""
    cfg = cfg or load_config()
    if not cfg.get('enabled'):
        return {'action': 'skipped', 'detail': ' predict.yaml enabled=false'}
    date = date or today_str(cfg)
    conn = get_conn()
    try:
        pend = conn.execute(
            'SELECT id, side, seq, theme, kind AS board_kind, board_code, '
            'params_fp FROM predictions WHERE trade_date=? AND status=? '
            'ORDER BY side, seq', (date, 'pending')).fetchall()
        if not pend:
            return {'date': date, 'action': 'no_pending', 'evaluated': 0,
                    'hit': {'leading': 0, 'risk': 0}}
        slot = _best_slot(conn, date)
        boards = snapshot_boards(conn, date, slot) if slot else []
        if not boards:
            return {'date': date, 'action': 'no_snapshot', 'evaluated': 0,
                    'hit': {'leading': 0, 'risk': 0},
                    'detail': ' 当日无收盘板块快照，pending 保留待下轮'}
        s = _run_params(cfg, 'settle')
        lead_need = int(s.get('leading_hit_rank') or 12)
        risk_need = int(s.get('risk_hit_rank') or 12)
        idx = _board_index(boards)
        ts = _now_iso()
        row_out = []
        stat = {'leading': {'total': 0, 'hit': 0, 'reversed': 0, 'missed': 0},
                'risk': {'total': 0, 'hit': 0, 'reversed': 0, 'missed': 0}}
        for r in pend:
            names = idx.get(r['board_kind'], {}).get('names') or []
            pct = idx.get(r['board_kind'], {}).get('pct', {}).get(r['theme'])
            total = len(names)
            rank = (names.index(r['theme']) + 1) if r['theme'] in names else None
            from_bottom = (total - rank + 1) if rank else None
            hit = False
            if rank is not None:
                if r['side'] == 'leading':
                    hit = rank <= lead_need
                else:
                    hit = (from_bottom or 0) <= risk_need
            reversed_ = bool(rank is not None and not hit)
            missed = rank is None
            key = 'leading' if r['side'] == 'leading' else 'risk'
            st = stat[key]
            st['total'] += 1
            if hit:
                st['hit'] += 1
            elif missed:
                st['missed'] += 1
            else:
                st['reversed'] += 1
            row_out.append({
                'id': r['id'], 'side': r['side'], 'seq': r['seq'],
                'theme': r['theme'], 'board_kind': r['board_kind'],
                'matched': bool(hit), 'hit': bool(hit),
                'reversed': bool(reversed_), 'missed': bool(missed),
                'vs_rank': rank, 'vs_rank_from_bottom': from_bottom,
                'vs_pct_chg': pct, 'status': 'evaluated'})
            conn.execute(
                'UPDATE predictions SET status=?, vs_outcome=?, updated_at=? '
                'WHERE id=?',
                ('evaluated',
                 json.dumps({k: v for k, v in row_out[-1].items()
                             if k not in ('id', 'side', 'seq', 'theme',
                                          'board_kind')}, ensure_ascii=False),
                 ts, r['id']))
        return _finish_settle(cfg, conn, date, slot, boards, row_out, stat, s, ts)
    finally:
        conn.close()


def _finish_settle(cfg, conn, date, slot, boards, row_out, stat, s, ts):
    """结算收尾：当日实际榜 topK 漏判 → predict_days 汇总 → commit。"""
    all_sorted = sorted(boards, key=lambda x: x['pct_chg'], reverse=True)
    k = int(s.get('top_k_actual') or 5)
    actual_leaders = [b['name'] for b in all_sorted[:k]]
    actual_risks = [b['name'] for b in all_sorted[-k:]][::-1]
    lead_hit = {o['theme'] for o in row_out
                if o['side'] == 'leading' and o['hit']}
    risk_hit = {o['theme'] for o in row_out
                if o['side'] == 'risk' and o['hit']}
    miss_lead = [n for n in actual_leaders if n not in lead_hit]
    miss_risk = [n for n in actual_risks if n not in risk_hit]
    st_l, st_r = stat['leading'], stat['risk']
    st_l['uncovered'] = miss_lead
    st_r['uncovered'] = miss_risk
    summary = {'settled_at': ts, 'slot': slot, 'top_k_actual': k,
               'actual_leading_top': actual_leaders,
               'actual_risk_top': actual_risks,
               'per_side': {'leading': st_l, 'risk': st_r}}
    old = conn.execute('SELECT summary FROM predict_days WHERE trade_date=?',
                       (date,)).fetchone()
    merged = dict(_parse_day_summary(old['summary'] if old else ''))
    merged.update(summary)
    _set_day(conn, date, {'settled_at': ts, 'status': 'settled',
                          'summary': json.dumps(merged, ensure_ascii=False),
                          'updated_at': ts})
    conn.commit()
    payload = {'date': date, 'action': 'settled', 'slot': slot,
               'evaluated': len(row_out), 'rows': row_out,
               'hit': {'leading': st_l['hit'], 'risk': st_r['hit']},
               'reversed': {'leading': st_l['reversed'],
                            'risk': st_r['reversed']},
               'missed': {'leading': st_l['missed'], 'risk': st_r['missed']},
               'uncovered': {'leading': miss_lead, 'risk': miss_risk}}
    log('  [结算] ' + date + ' evaluated=' + str(payload['evaluated'])
        + ' hit_l=' + str(st_l['hit']) + ' hit_r=' + str(st_r['hit']))
    return payload


def _window_stats(conn, cfg, date=None, window_days=None):
    """近 window_days 个已结算交易日滚动统计（命中率/错判/漏判均值）。"""
    r = _run_params(cfg, 'reflect')
    window_days = int(window_days or r.get('window_days') or 20)
    rows = conn.execute(
        'SELECT trade_date, summary FROM predict_days '
        'WHERE trade_date<=? AND status=? ORDER BY trade_date DESC LIMIT ?',
        (date, 'settled', window_days)).fetchall()[::-1]
    agg = {'leading': {'days': 0, 'total': 0, 'hit': 0, 'reversed': 0,
                       'missed': 0, 'uncovered_sum': 0},
           'risk': {'days': 0, 'total': 0, 'hit': 0, 'reversed': 0,
                    'missed': 0, 'uncovered_sum': 0}}
    detail = []
    for d in rows:
        ps = (_parse_day_summary(d['summary']).get('per_side') or {})
        entry = {'date': d['trade_date']}
        for side in ('leading', 'risk'):
            s = ps.get(side) or {}
            total = int(s.get('total') or 0)
            hit = int(s.get('hit') or 0)
            rev = int(s.get('reversed') or 0)
            miss = int(s.get('missed') or 0)
            unc = s.get('uncovered')
            unc_n = len(unc) if isinstance(unc, list) else int(unc or 0)
            a = agg[side]
            a['days'] += 1
            a['total'] += total
            a['hit'] += hit
            a['reversed'] += rev
            a['missed'] += miss
            a['uncovered_sum'] += unc_n
            entry[side] = {'total': total, 'hit': hit, 'reversed': rev,
                           'missed': miss, 'uncovered': unc_n}
        detail.append(entry)
    per = {}
    for side in ('leading', 'risk'):
        a = agg[side]
        per[side] = {
            'days': a['days'], 'total': a['total'], 'hit': a['hit'],
            'hit_rate': round(a['hit'] / a['total'], 4) if a['total'] else None,
            'reversed': a['reversed'], 'missed': a['missed'],
            'uncovered_avg': round(a['uncovered_sum'] / a['days'], 2)
            if a['days'] else None}
    return {'window_days': window_days,
            'date_from': detail[0]['date'] if detail else '',
            'date_to': detail[-1]['date'] if detail else '',
            'days': len(detail), 'per_side': per, 'detail': detail}


def _bounded(changes, bounds):
    """按 tunable_bounds 白名单 + 数值钳制过滤 LLM 建议。"""
    out = {}
    for path, value in (changes or {}).items():
        b = (bounds or {}).get(path)
        if not b:
            continue
        try:
            v = int(value) if b.get('type') == 'int' else float(value)
        except (TypeError, ValueError):
            continue
        lo, hi = float(b.get('min')), float(b.get('max'))
        v = max(lo, min(hi, v))
        out[path] = int(v) if b.get('type') == 'int' else v
    return out


def _fmt_side(entry):
    """一行可读的当日命中统计（用于喂给反思 LLM）。"""
    return ('总' + str(entry['total']) + ' 命中' + str(entry['hit'])
            + ' 错判' + str(entry['reversed']) + ' 漏判' + str(entry['uncovered']))


def reflect(cfg=None, date=None, window_days=None, no_llm=False, no_auto=False):
    """复盘：滚动统计 +（样本够时）LLM 反思输出有界参数建议并按 auto_tune 应用。"""
    cfg = cfg or load_config()
    if not cfg.get('enabled'):
        return {'rows': [], 'action': 'skipped', 'detail': ' predict.yaml enabled=false'}
    date = date or today_str(cfg)
    params = effective_params(cfg)
    old_fp = params_fp(params)
    r = _run_params(cfg, 'reflect')
    conn = get_conn()
    try:
        stats = _window_stats(conn, cfg, date=date, window_days=window_days)
        min_s = int(r.get('min_eval_samples') or 3)
        min_s = min(min_s, int(window_days or r.get('window_days') or 20))
        enough = stats['days'] >= min_s
    finally:
        conn.close()
    if not enough:
        return {'rows': [], 'action': 'insufficient', 'stats': stats,
                'detail': ' 已结算样本 ' + str(stats['days']) + ' < '
                          + str(min_s) + '，暂不反思'}
    dims = {'leading': stats['per_side']['leading'],
            'risk': stats['per_side']['risk'],
            'note': '复盘聚焦三个维度：命中（预测上榜）、错判（上榜但方向反）、'
                    '漏判（当日实际榜前K未预判/未命中）。'}
    model_used = 'rule'
    suggestions = []
    dims_text = str(dims)
    if (cfg.get('model', {}).get('enabled') and not no_llm):
        try:
            lines = []
            for row in stats['detail']:
                lines.append(row['date'] + ' 领涨[' + _fmt_side(row['leading'])
                             + '] 风险[' + _fmt_side(row['risk']) + ']')
            per = stats['per_side']
            header = ('滚动窗口 ' + str(stats['days']) + ' 个交易日（'
                      + stats['date_from'] + ' ~ ' + stats['date_to'] + '）')
            header += (' 领涨命中率 ' + str(per['leading']['hit_rate'])
                       + ' 风险命中率 ' + str(per['risk']['hit_rate']))
            bounds_txt = ' '.join(p + '[' + str(b.get('type')) + ' ' +
                                  str(b.get('min')) + '-' + str(b.get('max')) +
                                  ']' for p, b in
                                  (cfg.get('tunable_bounds') or {}).items())
            system = ('你是A股主题预测系统的复盘教练。基于近期滚动命中统计与典型错判/'
                      '漏判，输出参数优化建议。要求：克制、只对 tunable 白名单参数动手；'
                      '每次只建议最必要的少数几项，数值不超过给定边界；并分别点评'
                      '命中/错判/漏判三个复盘维度。')
            user = (header + chr(10) + chr(10).join(lines)
                    + chr(10) + '当前生效 params 指纹: ' + old_fp
                    + chr(10) + 'tunable 边界: ' + bounds_txt
                    + chr(10) + '只输出 JSON：'
                    + 'dims 为对象（含 leading/risk/reversed/missed 各维评述），'
                    + 'suggestions 为数组（元素 path/value/reason），path 必须在'
                    + 'tunable 边界列表内且数值在边界内。')
            m = cfg.get('model', {})
            txt = mi.chat_completion(system, user,
                                     temperature=float(r.get('temperature') or 0.3),
                                     max_tokens=int(r.get('max_tokens') or 1800),
                                     timeout=m.get('timeout_seconds'))
            data = _extract_json(txt)
            dims_text = json.dumps(data.get('dims') or dims,
                                   ensure_ascii=False)
            suggestions = data.get('suggestions') or []
            model_used = str(cfg.get('model', {}).get('backend') or 'deepseek')
        except Exception as e:  # noqa: BLE001 —— LLM 不可用仍保留规则复盘
            log('  LLM 反思失败，仅保留规则复盘: ' + type(e).__name__ + ': ' + str(e))
            model_used = 'rule'

    sug_map = {}
    for it in suggestions:
        if isinstance(it, dict):
            sug_map[str(it.get('path') or '')] = it.get('value')
    sug_map = {k: v for k, v in sug_map.items() if k}
    bounded = _bounded(sug_map, cfg.get('tunable_bounds') or {})
    skipped = sorted(set(sug_map) - set(bounded))
    auto = cfg.get('auto_tune', {}) or {}
    res = {}
    if bounded and auto.get('enabled', True) and not no_auto and not no_llm:
        keep = dict(list(bounded.items())[:int(auto.get('max_apply_per_run') or 3)])
        res = apply_param_changes(keep, reason='reflect auto-tune', source='reflect')
        log('  [反思自动应用] ' + date + ' ' + json.dumps(
            {'applied': keep, 'fp': res.get('new_fp')}, ensure_ascii=False))
    ts = _now_iso()
    conn = get_conn()
    try:
        cur = conn.execute(
            'INSERT INTO predict_reflections (trade_date, window_days, '
            'params_fp, summary, suggestions, applied, model, created_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (date, stats['window_days'], old_fp,
             json.dumps({'stats': stats, 'dims_text': dims_text},
                        ensure_ascii=False),
             json.dumps(sug_map, ensure_ascii=False),
             json.dumps({'applied': res, 'bounded': bounded, 'skipped': skipped},
                        ensure_ascii=False),
             model_used, ts))
        conn.commit()
        rid = cur.lastrowid
    finally:
        conn.close()
    return {'rows': [{'id': rid, 'trade_date': date, 'model': model_used}],
            'action': 'reflected', 'stats': stats,
            'suggestions': bounded, 'applied': res}


def _report_dir():
    """预测日结报告目录：PREDICT_REPORTS_DIR > market.yaml reports.dir > data/reports。"""
    env = os.environ.get('PREDICT_REPORTS_DIR')
    if env:
        return Path(env)
    sig = ms.load_config()
    d = ((sig.get('reports') or {}).get('dir')) if isinstance(sig, dict) else ''
    if d and str(d) != 'data/reports':
        return Path(str(d))
    return Path('data/reports')


def _write_report(date, sp, rp, cfg):
    """写 data/reports/predict_eval_YYYYMMDD.{json,md} + latest 副本。"""
    rep = {
        'date': date,
        'params_fp': params_fp(effective_params(cfg)),
        'settle': {k: sp.get(k) for k in
                   ('date', 'slot', 'evaluated', 'hit', 'reversed', 'missed',
                    'uncovered', 'rows') if k in sp},
        'reflect': {'days': (rp.get('stats') or {}).get('days'),
                    'window_days': (rp.get('stats') or {}).get('window_days'),
                    'per_side': (rp.get('stats') or {}).get('per_side'),
                    'suggestions': rp.get('suggestions')},
    }
    md = ['# 主题预测日结 ' + date, '',
          '- params_fp: ' + rep['params_fp'],
          '- 结算时点: ' + str(rep['settle'].get('slot')),
          '- 结算条数: ' + str(rep['settle'].get('evaluated', 0)), '']
    hit = rep['settle'].get('hit') or {}
    md.append('## 当日命中')
    md.append('领涨命中 ' + str(hit.get('leading', 0)) + ' / 风险命中 '
              + str(hit.get('risk', 0)))
    md.append('')
    for row in (sp.get('rows') or []):
        flag = 'HIT' if row['hit'] else ('REV' if row['reversed'] else 'MISS')
        side_cn = '领涨' if row['side'] == 'leading' else '风险'
        md.append('- [' + flag + '] ' + side_cn + ' ' + str(row['seq'])
                  + '. ' + str(row['theme']) + ' 当日同型榜第 '
                  + str(row['vs_rank']) + '（' + str(row['vs_pct_chg'])
                  + '%）')
    per = (rp.get('stats') or {}).get('per_side') or {}
    if per:
        md.append('')
        md.append('## 复盘滚动统计')
        for side, cn in (('leading', '领涨'), ('risk', '风险')):
            p = per.get(side) or {}
            md.append('- ' + cn + ': 样本 ' + str(p.get('days'))
                      + ' 天 命中率 ' + str(p.get('hit_rate')))
    dirp = _report_dir()
    dirp.mkdir(parents=True, exist_ok=True)
    base = 'predict_eval_' + date.replace('-', '')
    json_txt = json.dumps(rep, ensure_ascii=False, indent=2)
    md_txt = chr(10).join(md)
    for suffix, content in (('.json', json_txt), ('.md', md_txt)):
        (dirp / (base + suffix)).write_text(content, encoding='utf-8')
        (dirp / ('predict_eval_latest' + suffix)).write_text(
            content, encoding='utf-8')
    return {'md': str(dirp / (base + '.md')), 'json': str(dirp / (base + '.json'))}


def evening_stage(cfg=None, date=None, no_llm=False, no_auto=False):
    """evening 收尾编排：结算 → 复盘/反思 → 写日结报告；幂等可重跑。"""
    cfg = cfg or load_config()
    if not cfg.get('enabled'):
        return {'settle': {'action': 'skipped', 'detail': ' disabled'},
                'reflect': {'rows': []}, 'report': {}}
    date = date or today_str(cfg)
    sp = settle(cfg, date=date)
    rp = reflect(cfg, date=date, no_llm=no_llm, no_auto=no_auto)
    report = {}
    try:
        if (sp.get('evaluated') or 0) > 0:
            report = _write_report(date, sp, rp, cfg)
    except Exception as e:  # noqa: BLE001 —— 报告失败不影响结算/复盘
        log('  预测日结报告写出失败: ' + type(e).__name__ + ': ' + str(e))
    return {'settle': sp, 'reflect': rp, 'report': report}


def _mark_pending_missed(conn, date):
    """把该交易日仍处 pending 的预测行标为 missed，返回条数。"""
    n = conn.execute('SELECT COUNT(*) AS c FROM predictions '
                     'WHERE trade_date=? AND status=?',
                     (date, 'pending')).fetchone()['c']
    if n:
        conn.execute('UPDATE predictions SET status=?, updated_at=? '
                     'WHERE trade_date=? AND status=?',
                     ('missed', _now_iso(), date, 'pending'))
    return n


def _ensure_day_missed(conn, date, reason=''):
    """整天零预测时补写 predict_days.status='missed' 台账行（已存在则不动）。

    历史缺陷（2026-09-15 实例）：守护 06:44 宕机、12:24 才手工重启，08:40 的
    生成与 09:30 的兜底双双错过。tick 当时只把 missed 写进 predict_state.json，
    而该文件是**单日槽**（换日即被 state = {'date': ..., 'morning': {}} 覆写），
    于是这个交易日最终在库中彻底消失：predict_days 无行、predictions 无行，
    而当天 market_snapshot（230×3 行）与新闻数据齐全。后果是评估层无法区分
    「非交易日 / 守护宕机 / 生成失败」，反映射与命中率统计静默少一天样本
    （predict_days 只有 09-11/14/16/17 四天，漏掉 09-15）。
    """
    hit = conn.execute('SELECT status FROM predict_days WHERE trade_date=?',
                       (date,)).fetchone()
    if hit:
        return False
    cnt = conn.execute('SELECT COUNT(*) AS c FROM predictions WHERE trade_date=?',
                       (date,)).fetchone()['c']
    if cnt:
        return False        # 有预测行 → 不是「零生成」日，交给 status 正常流转
    _set_day(conn, date, {'status': 'missed', 'model': '', 'params_fp': '',
                          'summary': json.dumps(
                              {'missed_reason': reason or 'no_generation',
                               'marked_at': _now_iso()},
                              ensure_ascii=False)})
    log('  [missed] ' + date + ' 当日零预测记录，补 predict_days 台账行（'
        + (reason or 'n/a') + '）')
    return True


def _mark_day_missed(date, reason=''):
    """tick 判定整日 missed 时的落库入口（pending 行 + 台账行）。"""
    conn = get_conn()
    try:
        n = _mark_pending_missed(conn, date)
        ledger = _ensure_day_missed(conn, date, reason)
        conn.commit()
    finally:
        conn.close()
    return {'pending_marked': n, 'ledger': ledger}


def _backfill_missed(prev, today):
    """把 prev~today 之间「零预测」或「仍 pending」的过去交易日记 missed。

    历史缺陷：原实现只把「已存在且 status='pending'」的预测行改成 missed ——
    整天没生成时两侧都没有行，一个 UPDATE 影响 0 行，既不报错也不留痕；叠加
    tick 的 missed 决策只写单日槽状态文件，该交易日就此静默丢失。现补齐台账行
    （_ensure_day_missed），使漏日可审计、可统计。
    """
    try:
        between = ms.trading_days_between(prev, today) or []
    except Exception:  # noqa: BLE001
        between = [prev]
    conn = get_conn()
    try:
        for d in between:
            if d >= today:
                continue
            n = _mark_pending_missed(conn, d)
            if n:
                log('  [missed] ' + d + ' 已过盘前窗口，' + str(n)
                    + ' 条预测标 missed（不再结算）')
            _ensure_day_missed(conn, d, 'backfill')
        conn.commit()
    finally:
        conn.close()


def tick(cfg=None, date=None, force=False):
    """守护轮询入口：交易日 due~deadline 生成 / allow_late_until 兜底 / 超时记 missed。"""
    cfg = cfg or load_config()
    if not cfg.get('enabled'):
        return [{'action': 'skipped', 'detail': ' predict.yaml disabled'}]
    now = now_local(cfg)
    date = date or now.strftime('%Y-%m-%d')
    mn = cfg.get('morning') or {}
    due = _hhmm(mn.get('due') or '08:40')
    deadline = _hhmm(mn.get('deadline') or '09:10')
    allow = _hhmm(mn.get('allow_late_until') or '09:30')
    cur = now.hour * 60 + now.minute
    trading = bool(ms.trading_days_between(date, date) == [date])
    state = load_state()
    if state.get('date') and state.get('date') != date:
        _backfill_missed(state['date'], date)
    if state.get('date') != date:
        state = {'date': date, 'morning': {}}
    morning = state.setdefault('morning', {})
    if not trading:
        state['date'] = date
        save_state(state)
        return [{'action': 'skipped', 'detail': ' 非交易日 ' + date}]
    if morning.get('decided'):
        state['date'] = date
        save_state(state)
        return []
    conn = get_conn()
    try:
        cnt = conn.execute('SELECT COUNT(*) AS c FROM predictions '
                           'WHERE trade_date=?', (date,)).fetchone()['c']
    finally:
        conn.close()
    if cnt:
        morning['generated'] = True
        morning['decided'] = True
        save_state(state)
        return [{'action': 'done', 'detail': ' ' + date + ' 已有 '
                  + str(cnt) + ' 条预测（幂等，不重复生成）'}]
    if cur < due:
        state['date'] = date
        save_state(state)
        return [{'action': 'idle', 'detail': ' 未到盘前预测窗口（'
                  + str(mn.get('due') or '08:40') + '）'}]
    if cur > allow:
        res = _mark_day_missed(date, 'over_deadline_'
                               + str(mn.get('allow_late_until') or '09:30'))
        morning['generated'] = False
        morning['decided'] = True
        morning['action'] = 'missed'
        state['date'] = date
        save_state(state)
        return [{'action': 'missed', 'detail': ' 已超过兜底 '
                  + str(mn.get('allow_late_until') or '09:30')
                  + '，今日盘前预测记 missed（台账已落库'
                  + ('，另标 ' + str(res['pending_marked']) + ' 条 pending'
                     if res['pending_marked'] else '') + '）'}]
    r = generate_day(cfg, date=date, force=force)
    if r.get('action') == 'generated':
        action = 'done' if cur <= deadline else 'late'
        morning['generated'] = True
        morning['decided'] = True
        morning['action'] = action
        state['date'] = date
        save_state(state)
        return [{'action': action, 'detail': ' ' + date + ' basis='
                  + str(r.get('basis_date')) + ' model=' + str(r.get('model'))
                  + ' fp=' + str(r.get('params_fp'))}]
    return [r]


# CLI（调试/人工补跑入口）
def _build_argparser():
    p = argparse.ArgumentParser(
        prog='predict.py',
        description='需求3：盘前主题预测→收盘复盘→LLM反思闭环（生成/结算/复盘/回滚）')
    sub = p.add_subparsers(dest='cmd', required=True)

    g = sub.add_parser('generate', help='直接生成指定日期预测（不受时间窗限制）')
    g.add_argument('--date')
    g.add_argument('--basis')
    g.add_argument('--force', action='store_true')
    g.add_argument('--no-llm', action='store_true')

    m = sub.add_parser('morning', help='守护语义：到点生成/兜底/超时 missed')
    m.add_argument('--date')
    m.add_argument('--force', action='store_true')

    s = sub.add_parser('settle', help='收盘结算当日预测（回填 vs_outcome）')
    s.add_argument('--date')

    e = sub.add_parser('evening', help='evening 收尾 = settle + reflect + 报告')
    e.add_argument('--date')
    e.add_argument('--no-llm', action='store_true')
    e.add_argument('--no-auto', action='store_true', help='禁止自动调参应用')

    r = sub.add_parser('reflect', help='窗口统计 + LLM 反思')
    r.add_argument('--date')
    r.add_argument('--window', type=int, help='滚动窗口交易日数')
    r.add_argument('--no-llm', action='store_true')
    r.add_argument('--no-auto', action='store_true', help='禁止自动调参应用')

    rb = sub.add_parser('rollback', help='按参数指纹回滚生效参数')
    rb.add_argument('--fp', required=True, help='目标指纹（前16位或其前缀）')
    rb.add_argument('--reason', default='manual rollback')

    sub.add_parser('params', help='查看当前生效参数与指纹')
    sub.add_parser('status', help='查看当日预测/结算状态')
    return p


def _status_payload(cfg, date):
    conn = get_conn()
    try:
        pend = conn.execute('SELECT COUNT(*) AS c FROM predictions '
                            'WHERE trade_date=? AND status=?',
                            (date, 'pending')).fetchone()['c']
        evaled = conn.execute('SELECT COUNT(*) AS c FROM predictions '
                              'WHERE trade_date=? AND status=?',
                              (date, 'evaluated')).fetchone()['c']
        days = conn.execute('SELECT trade_date, status, model, params_fp, '
                            'settled_at FROM predict_days WHERE trade_date=?',
                            (date,)).fetchone()
        return {'date': date, 'predictions_pending': pend,
                'predictions_evaluated': evaled,
                'day': dict(days) if days else None}
    finally:
        conn.close()


def _params_payload(cfg):
    params = effective_params(cfg)
    conn = get_conn()
    try:
        hist = conn.execute('SELECT fp, parent_fp, reason, source, created_at '
                            'FROM predict_param_history ORDER BY id DESC '
                            'LIMIT 8').fetchall()
        return {'fp': params_fp(params), 'effective': params,
                'history': [dict(h) for h in hist]}
    finally:
        conn.close()


def _main(argv=None):
    args = _build_argparser().parse_args(argv)
    ensure_schema()
    cfg = load_config()
    date = getattr(args, 'date', None) or today_str(cfg)
    if args.cmd == 'generate':
        out = generate_day(cfg, date=date, basis_date=args.basis,
                           force=args.force, no_llm=args.no_llm)
    elif args.cmd == 'morning':
        out = tick(cfg, date=date, force=args.force)
    elif args.cmd == 'settle':
        out = settle(cfg, date=date)
    elif args.cmd == 'evening':
        out = evening_stage(cfg, date=date, no_llm=args.no_llm,
                            no_auto=args.no_auto)
    elif args.cmd == 'reflect':
        out = reflect(cfg, date=date, window_days=args.window,
                      no_llm=args.no_llm, no_auto=args.no_auto)
    elif args.cmd == 'rollback':
        out = rollback_to(args.fp, reason=args.reason)
    elif args.cmd == 'params':
        out = _params_payload(cfg)
    elif args.cmd == 'status':
        out = _status_payload(cfg, date)
    else:
        raise SystemExit('unknown command: ' + str(args.cmd))
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(_main())


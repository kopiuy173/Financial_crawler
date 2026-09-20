#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""predict_baseline.py — 需求3 评估层 P0：只读基线对照（解析随机 / 惯性 / 反转）"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import predict as pr  # noqa: E402  复用连接、配置、快照读取与依据日解析

KINDS = ("industry_board", "concept_board")


def log(msg):
    print(msg, flush=True)


def report_dir():
    """报告目录：BASELINE_REPORTS_DIR > market.yaml reports.dir > 仓库 data/reports。

    与 predict.py 的解析顺序一致，确保所有报告统一落同一目录。
    """
    env = os.environ.get("BASELINE_REPORTS_DIR")
    if env:
        d = Path(env)
    else:
        d = None
        try:
            import yaml
            cfg = yaml.safe_load((HERE / "config" / "market.yaml").read_text(
                encoding="utf-8")) or {}
            rd = ((cfg.get("reports") or {}).get("dir")) or ""
            if rd and str(rd) != "data/reports":
                d = Path(str(rd))
        except Exception:  # noqa: BLE001
            d = None
        if d is None:
            d = Path("data/reports")
    d.mkdir(parents=True, exist_ok=True)
    return d


def settle_params(cfg):
    """结算口径：leading_hit_rank / risk_hit_rank / top_k_actual。

    必须取**生效参数**：否则基线对照用 yaml 基线、predict.settle 用覆盖值，
    两边口径分叉，「与 predict.settle 逐行对齐」的前提被破坏。
    """
    s = pr._run_params(cfg, "settle")
    return (int(s.get("leading_hit_rank") or 12),
            int(s.get("risk_hit_rank") or 12),
            int(s.get("top_k_actual") or 5))


def gen_params(cfg):
    """生成口径：每天领涨与风险条数，用于基线取同样条数（取生效参数）。"""
    g = pr._run_params(cfg, "generation")
    return int(g.get("n_leading") or 3), int(g.get("n_risk") or 2)


def classify(index, kind, theme, side, lead_need, risk_need):
    """与 predict.settle 逐行对齐的判定。"""
    group = index.get(kind) or {}
    names = group.get("names") or []
    pct = (group.get("pct") or {}).get(theme)
    total = len(names)
    rank = (names.index(theme) + 1) if theme in names else None
    from_bottom = (total - rank + 1) if rank else None
    hit = False
    if rank is not None:
        if side == "leading":
            hit = rank <= lead_need
        else:
            hit = (from_bottom or 0) <= risk_need
    return {
        "kind": kind, "side": side, "theme": theme,
        "hit": bool(hit),
        "reversed": bool(rank is not None and not hit),
        "missed": rank is None,
        "vs_rank": rank,
        "vs_rank_from_bottom": from_bottom,
        "vs_pct_chg": pct,
    }


def names_desc(index, kind):
    return ((index.get(kind) or {}).get("names") or [])


def pick_by_direction(index, kind, side, seq, reverse=False):
    """按名次取基线主题；reverse=True 时反向（用于反转基线）。"""
    names = names_desc(index, kind)
    if not names or seq < 1 or seq > len(names):
        return None
    if side == "leading":
        return names[-seq] if reverse else names[seq - 1]
    return names[seq - 1] if reverse else names[-seq]


def actual_topk(boards, k):
    """当日全部板块按涨跌幅排序：前 K 为实际领涨，后 K 为实际风险（与 predict._finish_settle 一致）。"""
    ordered = sorted(boards, key=lambda x: x.get("pct_chg") or 0.0, reverse=True)
    leaders = [b["name"] for b in ordered[:k]]
    risks = [b["name"] for b in ordered[-k:]][::-1]
    return leaders, risks


def coverage(detail, actual_leaders, actual_risks):
    lead_hit = {r["theme"] for r in detail if r["side"] == "leading" and r["hit"]}
    risk_hit = {r["theme"] for r in detail if r["side"] == "risk" and r["hit"]}
    return ([n for n in actual_leaders if n not in lead_hit],
            [n for n in actual_risks if n not in risk_hit])


def summarize(rows, index, lead_need, risk_need, actual_leaders, actual_risks):
    stat = {"leading": {"total": 0, "hit": 0, "reversed": 0, "missed": 0},
            "risk": {"total": 0, "hit": 0, "reversed": 0, "missed": 0}}
    detail = []
    for kind, side, theme in rows:
        if not theme:
            continue
        r = classify(index, kind, theme, side, lead_need, risk_need)
        st = stat[r["side"]]
        st["total"] += 1
        st["hit"] += 1 if r["hit"] else 0
        st["missed"] += 1 if r["missed"] else 0
        st["reversed"] += 1 if r["reversed"] else 0
        detail.append(r)
    miss_lead, miss_risk = coverage(detail, actual_leaders, actual_risks)
    total = stat["leading"]["total"] + stat["risk"]["total"]
    hits = stat["leading"]["hit"] + stat["risk"]["hit"]
    return {"rows": len(detail), "hits": hits,
            "hit_rate": round(hits / total, 4) if total else None,
            "per_side": stat,
            "uncovered": {"leading": miss_lead, "risk": miss_risk},
            "uncovered_n": len(miss_lead) + len(miss_risk),
            "detail": detail}


def load_index(conn, trade_date):
    """读取指定交易日的收盘快照并建索引；返回 slot / boards / index。"""
    slot = pr._best_slot(conn, trade_date)
    if not slot:
        return None, [], {}
    boards = pr.snapshot_boards(conn, trade_date, slot)
    if not boards:
        return slot, [], {}
    return slot, boards, pr._board_index(boards)


def eval_day(conn, cfg, trade_date, require_close=True):
    """评估单个交易日：解析随机期望 + 惯性 + 反转 + 实测（如有预测行）。"""
    lead_need, risk_need, top_k = settle_params(cfg)
    n_lead, n_risk = gen_params(cfg)
    slot, boards, index = load_index(conn, trade_date)
    if not boards:
        return {"trade_date": trade_date, "status": "no_snapshot",
                "detail": "当日无板块快照，跳过"}
    if require_close and slot != "15:00":
        return {"trade_date": trade_date, "status": "no_close_snapshot",
                "slot": slot,
                "detail": "当日最佳快照时点为 " + str(slot) +
                          "，评估需收盘快照（可用 --allow-intraday 放宽）"}
    actual_leaders, actual_risks = actual_topk(boards, top_k)
    basis = pr.resolve_basis_date(conn, trade_date)
    out = {"trade_date": trade_date, "status": "ok", "slot": slot,
           "is_close": slot == "15:00",
           "basis_date": basis,
           "board_counts": {k: len(names_desc(index, k)) for k in KINDS},
           "thresholds": {"leading_hit_rank": lead_need,
                          "risk_hit_rank": risk_need, "top_k_actual": top_k},
           "picks_per_day": {"n_leading": n_lead, "n_risk": n_risk},
           "actual_top": {"leaders": actual_leaders, "risks": actual_risks},
           "analytic_random": {}, "strategies": {}}
    for k in KINDS:
        n = len(names_desc(index, k))
        if n:
            out["analytic_random"][k] = {
                "n_boards": n,
                "exp_leading_hit_rate": round(min(lead_need, n) / n, 4),
                "exp_risk_hit_rate": round(min(risk_need, n) / n, 4)}
    if basis:
        bslot, _bboards, bidx = load_index(conn, basis)
        out["basis_slot"] = bslot
        for strat, rev in (("momentum", False), ("reversal", True)):
            res = {}
            for k in KINDS:
                rows = ([(k, "leading",
                          pick_by_direction(bidx, k, "leading", i + 1, rev))
                         for i in range(n_lead)]
                        + [(k, "risk",
                            pick_by_direction(bidx, k, "risk", i + 1, rev))
                           for i in range(n_risk)])
                res[k] = summarize(rows, index, lead_need, risk_need,
                                   actual_leaders, actual_risks)
            out["strategies"][strat] = res
    else:
        out["basis_note"] = "前溯 14 个交易日内未找到带板块快照的依据日"
    prows = conn.execute(
        "SELECT kind, side, theme FROM predictions WHERE trade_date=? "
        "ORDER BY side, seq", (trade_date,)).fetchall()
    if prows:
        rows = [(r["kind"] or "", r["side"], r["theme"]) for r in prows]
        out["strategies"]["actual_predict"] = {
            "all": summarize(rows, index, lead_need, risk_need,
                             actual_leaders, actual_risks)}
    else:
        out["actual_note"] = "当日无预测行（未生成或记 missed）"
    return out


BASELINE_LOG_NAME = "baseline_daily_log.md"

BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS predict_baselines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date    TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    rows          INTEGER DEFAULT 0,
    hits          INTEGER DEFAULT 0,
    hit_rate      REAL,
    uncovered_n   INTEGER,
    leading_hits  INTEGER DEFAULT 0,
    leading_total INTEGER DEFAULT 0,
    risk_hits     INTEGER DEFAULT 0,
    risk_total    INTEGER DEFAULT 0,
    basis_date    TEXT,
    slot          TEXT,
    thresholds    TEXT DEFAULT '',
    detail        TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    UNIQUE (trade_date, strategy, kind)
);
CREATE INDEX IF NOT EXISTS idx_pb_date ON predict_baselines(trade_date);
"""


def ensure_baseline_schema(conn):
    conn.executescript(BASELINE_SCHEMA)
    conn.commit()


def _baseline_rows(day):
    out = []
    for kind, ar in (day.get("analytic_random") or {}).items():
        out.append({"strategy": "analytic_random", "kind": kind,
                    "rows": 0, "hits": 0,
                    "hit_rate": ar.get("exp_leading_hit_rate"),
                    "uncovered_n": None, "per_side": {}, "detail": ar})
    for strat in ("momentum", "reversal"):
        for kind, s2 in (day.get("strategies", {}).get(strat) or {}).items():
            out.append({"strategy": strat, "kind": kind,
                        "rows": s2.get("rows"), "hits": s2.get("hits"),
                        "hit_rate": s2.get("hit_rate"),
                        "uncovered_n": s2.get("uncovered_n"),
                        "per_side": s2.get("per_side") or {},
                        "detail": {"uncovered": s2.get("uncovered")}})
    ap = (day.get("strategies") or {}).get("actual_predict")
    if ap:
        s3 = ap.get("all") or {}
        out.append({"strategy": "actual_predict", "kind": "mix",
                    "rows": s3.get("rows"), "hits": s3.get("hits"),
                    "hit_rate": s3.get("hit_rate"),
                    "uncovered_n": s3.get("uncovered_n"),
                    "per_side": s3.get("per_side") or {},
                    "detail": {"uncovered": s3.get("uncovered")}})
    return out


def save_rows(conn, day):
    """按 trade_date 覆盖写入（幂等）。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM predict_baselines WHERE trade_date=?",
                 (day["trade_date"],))
    rows = _baseline_rows(day)
    for r in rows:
        ps = r.get("per_side") or {}
        lead = ps.get("leading") or {}
        risk = ps.get("risk") or {}
        conn.execute(
            "INSERT OR REPLACE INTO predict_baselines "
            "(trade_date, strategy, kind, rows, hits, hit_rate, uncovered_n, "
            " leading_hits, leading_total, risk_hits, risk_total, basis_date, "
            " slot, thresholds, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (day["trade_date"], r["strategy"], r["kind"], r.get("rows") or 0,
             r.get("hits") or 0, r.get("hit_rate"), r.get("uncovered_n"),
             lead.get("hit") or 0, lead.get("total") or 0,
             risk.get("hit") or 0, risk.get("total") or 0,
             day.get("basis_date"), day.get("slot"),
             json.dumps(day.get("thresholds") or {}, ensure_ascii=False),
             json.dumps(r.get("detail") or {}, ensure_ascii=False), ts))
    conn.commit()
    return len(rows)


def append_daily_log(conn, day):
    """基线台账每日一行（同日已存在则不重复追加）。"""
    path = report_dir() / BASELINE_LOG_NAME
    header = ["# 基线台账（每日一行，机器生成）", "",
              "| 日期 | 实测 | 惯性行业 | 惯性概念 | 反转行业 | 反转概念 | "
              "随机行业 | 随机概念 | 样本天数 |",
              "|---|---|---|---|---|---|---|---|---|"]
    if not path.exists():
        path.write_text("\n".join(header) + "\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if "| " + str(day["trade_date"]) + " |" in text:
        return False

    def cell(x):
        if not x:
            return "-"
        return str(x.get("hits")) + "/" + str(x.get("rows"))

    st = day.get("strategies") or {}
    ar = day.get("analytic_random") or {}
    samples = conn.execute(
        "SELECT COUNT(DISTINCT trade_date) c FROM predict_baselines"
    ).fetchone()["c"]
    ai = ar.get("industry_board") or {}
    ac = ar.get("concept_board") or {}
    line = ("| " + str(day["trade_date"]) +
            " | " + cell((st.get("actual_predict") or {}).get("all")) +
            " | " + cell((st.get("momentum") or {}).get("industry_board")) +
            " | " + cell((st.get("momentum") or {}).get("concept_board")) +
            " | " + cell((st.get("reversal") or {}).get("industry_board")) +
            " | " + cell((st.get("reversal") or {}).get("concept_board")) +
            " | " + (str(ai.get("exp_leading_hit_rate")) if ai else "-") +
            " | " + (str(ac.get("exp_leading_hit_rate")) if ac else "-") +
            " | " + str(samples) + " |")
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return True


def run_for_date(trade_date, cfg=None, write_db=True, write_log=True):
    """调度器入口：评估单日并可选写库与台账，返回紧凑摘要。"""
    cfg = cfg or pr.load_config()
    conn = pr.get_conn()
    try:
        ensure_baseline_schema(conn)
        day = eval_day(conn, cfg, trade_date)
        if day.get("status") != "ok":
            return {"date": trade_date, "status": day.get("status")}
        if write_db:
            save_rows(conn, day)
        if write_log:
            append_daily_log(conn, day)
        st = day.get("strategies") or {}
        return {"date": trade_date, "status": "ok",
                "actual": ((st.get("actual_predict") or {}).get("all") or {}).get("hit_rate"),
                "momentum_ind": ((st.get("momentum") or {}).get("industry_board") or {}).get("hit_rate"),
                "momentum_con": ((st.get("momentum") or {}).get("concept_board") or {}).get("hit_rate"),
                "reversal_ind": ((st.get("reversal") or {}).get("industry_board") or {}).get("hit_rate"),
                "reversal_con": ((st.get("reversal") or {}).get("concept_board") or {}).get("hit_rate")}
    finally:
        conn.close()


def md_table(results, run_at):
    """生成 Markdown 对照表。"""
    lines = ["# 基线对照（评估层 P0，只读）", "",
             "- 运行时间：" + run_at,
             "- 口径：与 predict.settle 一致（同 kind 定位、leading 取涨幅榜前 N、"
             "risk 取跌幅榜前 N、未上榜记 missed、上榜未达阈值记 reversed）",
             "- 解析随机为期望命中率；惯性取依据日涨幅榜前 N；反转取依据日跌幅榜前 N；"
             "实测来自 predictions 表",
             ""]
    for r in results:
        lines.append("## " + str(r["trade_date"]) + "（" + str(r.get("status")) + "）")
        lines.append("")
        if r.get("status") != "ok":
            lines.append("- " + str(r.get("detail") or ""))
            lines.append("")
            continue
        th = r["thresholds"]
        lines.append("- 快照时点：" + str(r.get("slot")) +
                     "；依据日：" + str(r.get("basis_date")))
        lines.append("- 板块数：行业 " + str(r["board_counts"].get("industry_board")) +
                     "，概念 " + str(r["board_counts"].get("concept_board")))
        lines.append("- 阈值：leading " + str(th["leading_hit_rank"]) +
                     "，risk " + str(th["risk_hit_rank"]) +
                     "，top_k_actual " + str(th["top_k_actual"]))
        lines.append("")
        lines.append("| 策略 | 类别 | 条数 | 命中 | 命中率 | 漏判数 |")
        lines.append("|---|---|---|---|---|---|")
        for k in KINDS:
            ar = r["analytic_random"].get(k)
            if ar:
                lines.append("| 解析随机 | " + k + " | - | - | " +
                             str(ar["exp_leading_hit_rate"]) + " 领涨 / " +
                             str(ar["exp_risk_hit_rate"]) + " 风险 | - |")
        for strat in ("momentum", "reversal"):
            for k, s in (r["strategies"].get(strat) or {}).items():
                lines.append("| " + strat + " | " + k + " | " + str(s["rows"]) +
                             " | " + str(s["hits"]) + " | " + str(s["hit_rate"]) +
                             " | " + str(s["uncovered_n"]) + " |")
        ap = r["strategies"].get("actual_predict")
        if ap:
            s = ap["all"]
            lines.append("| 实测预测 | 混合 | " + str(s["rows"]) + " | " +
                         str(s["hits"]) + " | " + str(s["hit_rate"]) + " | " +
                         str(s["uncovered_n"]) + " |")
        else:
            lines.append("")
            lines.append("- 实测：" + str(r.get("actual_note") or "无"))
        lines.append("")
    return "\n".join(lines) + "\n"


def recent_dates(conn, n=3):
    """库中最近 n 个**真实交易日**（且含 15:00 板块快照）的日期，升序。

    历史缺陷：只按 market_snapshot 的 DISTINCT trade_date 取数，而该表曾被
    无交易日守卫的快照任务写入「周末重盖章行」（2026-09-12/13 的值 ≈ 09-11），
    于是 09-13（周日）被当成交易日参与基线评估：惯性策略依据日仍是 09-11、
    「次日实际榜」却是同一份数据，5/5 满分纯属同义反复。此处以 akshare 交易日
    历（ms.is_trade_day）为准做二次过滤；日历不可用时 is_trade_day 返回 True，
    退化为原行为。
    """
    n = int(n)
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM market_snapshot WHERE slot='15:00' "
        "ORDER BY trade_date DESC LIMIT ?", (max(n * 3, n),)).fetchall()
    out = []
    for r in rows:
        try:
            if not pr.ms.is_trade_day(r["trade_date"]):
                continue
        except Exception:  # noqa: BLE001 日历不可用时不拦，保持原行为
            pass
        out.append(r["trade_date"])
        if len(out) >= n:
            break
    return out[::-1]


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="predict_baseline",
        description="需求3 评估层 P0：只读基线对照（解析随机 / 惯性 / 反转）")
    parser.add_argument("--date", default=None, help="单个交易日 YYYY-MM-DD")
    parser.add_argument("--dates", default=None,
                        help="逗号分隔的交易日，如 2026-09-08,2026-09-09")
    parser.add_argument("--allow-intraday", action="store_true",
                        help="允许用非收盘快照评估（默认仅用 15:00）")
    parser.add_argument("--no-db", action="store_true",
                        help="只出报告，不写 predict_baselines 表")
    parser.add_argument("--no-log", action="store_true",
                        help="不追加 baseline_daily_log.md")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    cfg = pr.load_config()
    conn = pr.get_conn()
    try:
        if args.dates:
            dates = [d.strip() for d in args.dates.split(",") if d.strip()]
        elif args.date:
            dates = [args.date.strip()]
        else:
            dates = recent_dates(conn, 3)
        results = [eval_day(conn, cfg, d,
                            require_close=not args.allow_intraday)
                   for d in dates]
    finally:
        conn.close()

    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d = report_dir()
    samples = None
    conn2 = pr.get_conn()
    try:
        ensure_baseline_schema(conn2)
        for r in results:
            if r.get("status") == "ok" and not args.no_db:
                save_rows(conn2, r)
        for r in results:
            if r.get("status") == "ok" and not args.no_log:
                append_daily_log(conn2, r)
        samples = conn2.execute(
            "SELECT COUNT(DISTINCT trade_date) c FROM predict_baselines"
        ).fetchone()["c"]
    finally:
        conn2.close()
    for r in results:
        if r.get("status") == "ok":
            name = "baseline_eval_" + str(r["trade_date"]).replace("-", "") + ".json"
            (d / name).write_text(json.dumps(r, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    if samples is not None:
        log("predict_baselines 累计交易日: " + str(samples))
    (d / "baseline_eval_latest.md").write_text(md_table(results, run_at),
                                               encoding="utf-8")
    if not args.quiet:
        for r in results:
            if r.get("status") != "ok":
                log(str(r["trade_date"]) + "  " + str(r.get("status")) + "  " +
                    str(r.get("detail") or ""))
                continue
            log(str(r["trade_date"]) + "  analytic_random " +
                json.dumps(r["analytic_random"], ensure_ascii=False))
            for strat in ("momentum", "reversal"):
                for k, s in (r["strategies"].get(strat) or {}).items():
                    log(str(r["trade_date"]) + "  " + strat + "  " + k +
                        "  rows=" + str(s["rows"]) + " hits=" + str(s["hits"]) +
                        " rate=" + str(s["hit_rate"]) +
                        " uncovered=" + str(s["uncovered_n"]))
            ap = r["strategies"].get("actual_predict")
            if ap:
                log(str(r["trade_date"]) + "  actual  rows=" + str(ap["all"]["rows"]) +
                    " hits=" + str(ap["all"]["hits"]) +
                    " rate=" + str(ap["all"]["hit_rate"]))
        log("报告已写入：" + str(d / "baseline_eval_latest.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())



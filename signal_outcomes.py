#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""signal_outcomes.py — 需求 2：新闻信号 → 结果结算 → 回测评估面板"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

_PROJ = Path(__file__).resolve().parent
os.chdir(_PROJ)

import database as db  # noqa: E402
import market_snapshot as ms  # noqa: E402  （复用 log/配置/交易日历）

CONFIG = ms.load_config()


def log(msg):
    ms.log(msg)


def now_local(cfg=None):
    return ms.now_local(cfg or CONFIG)


def get_db_path():
    return os.environ.get("KB_DB_PATH") or db.get_db_path()


def get_conn():
    return db.get_connection(get_db_path())

# 发布时刻解析 + 方向词表
def _epoch_to_local(ts):
    """秒/毫秒时间戳 → Asia/Shanghai datetime；失败返回 None。"""
    try:
        ts = float(str(ts).strip())
        if ts > 1e12:
            ts /= 1000.0
        tz = ZoneInfo("Asia/Shanghai") if ZoneInfo is not None else None
        return datetime.fromtimestamp(ts, tz=tz).replace(tzinfo=None)
    except (ValueError, OSError, OverflowError, TypeError):
        return None


def publish_dt(doc):
    """从文档 metadata 尽量还原发布时刻（本地 naive datetime），失败 None。"""
    meta = doc.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except ValueError:
            meta = {}
    meta = meta if isinstance(meta, dict) else {}
    for key in ("firstPublishTime", "ctime", "intime", "pub_time",
                "publish_time", "publishTime"):
        if meta.get(key):
            dt = _epoch_to_local(meta[key])
            if dt:
                return dt
    hhmm = str(meta.get("date") or "")
    if ":" in hhmm:
        try:
            hh, mm = hhmm.strip().split(":")[:2]
            pub = doc.get("publish_date") or now_local(CONFIG).strftime("%Y-%m-%d")
            return datetime.strptime(pub, "%Y-%m-%d").replace(
                hour=int(hh) % 24, minute=int(mm) % 60)
        except (TypeError, ValueError):
            pass
    return None


def lexicon_direction(cfg, title):
    """标题利好/利空词表 → up / down / neutral。"""
    lex = (cfg.get("signal") or {}).get("lexicon") or {}
    up = [w for w in lex.get("up", []) if w and w in title]
    down = [w for w in lex.get("down", []) if w and w in title]
    if len(up) > len(down):
        return "up"
    if len(down) > len(up):
        return "down"
    return "neutral"


def _has_signal(conn, news_id, symbol, horizon):  # 兼容旧引用（已无调用方）
    row = conn.execute(
        "SELECT 1 FROM signal_outcomes WHERE news_id=? AND symbol=? "
        "AND horizon_days=?", (news_id, symbol, int(horizon))).fetchone()
    return row is not None

# 1) 建信号行
def build_signals(conn, cfg=None, publish_date=None):
    """把已关联的新闻×标的 展开成各 horizon 的信号行（已存在则自动忽略）。

    批量 INSERT OR IGNORE（add_signals_batch 单事务）替代原逐行查重+逐行
    upsert，每日几百~几千条展开从 O(N) 次事务降为 1 次。
    """
    cfg = cfg or CONFIG
    if not (cfg.get("signal") or {}).get("enabled", True):
        return 0
    horizons = [int(h) for h in (cfg.get("signal") or {}).get("horizons", [])]
    bench_symbol = (cfg.get("benchmark") or {}).get("symbol") or "000300.SH"
    linked = db.news_ticker_rows(conn, publish_date=publish_date, limit=20000)
    cands = []
    for row in linked:
        direction = lexicon_direction(cfg, str(row.get("title") or ""))
        pt = publish_dt(row)
        sig_time = pt.strftime("%H:%M:%S") if pt else ""
        for h in horizons:
            cands.append({
                "news_id": row["news_id"], "symbol": row["symbol"],
                "horizon_days": h, "direction": direction,
                "direction_method": "lexicon",
                "signal_date": row.get("publish_date") or "",
                "signal_time": sig_time, "bench_symbol": bench_symbol,
            })
    added = db.add_signals_batch(conn, cands) if cands else 0
    scope = publish_date or "全部"
    log(f" 建信号 {scope}：候选 {len(cands)} 条，新增 {added} 条")
    return added


_DFX = None


def _data_fetcher():
    """复用 dify_migration/python/data_fetcher 的 akshare 日线接入。"""
    global _DFX
    if _DFX is None:
        sys.path.insert(0, str(_PROJ / "dify_migration" / "python"))
        import data_fetcher as _m  # noqa: PLC0415
        _DFX = _m
    return _DFX


def _ak_index_suffix(symbol):
    code = symbol.split(".")[0]
    exch = symbol.rsplit(".", 1)[-1].lower()
    return {"sh": f"sh{code}", "sz": f"sz{code}", "bj": f"bj{code}"}.get(
        exch, f"sh{code}")


def _bench_rows_from_df(df, symbol, source, start, end):
    out = []
    for _, r in df.iterrows():
        d = str(r.get("date") or r.get("trade_date") or "")[:10]
        if not start <= d <= end:
            continue
        out.append({
            "symbol": symbol, "source": source, "trade_date": d,
            "open": ms._fnum(r.get("open")), "high": ms._fnum(r.get("high")),
            "low": ms._fnum(r.get("low")), "close": ms._fnum(r.get("close")),
            "volume": ms._fnum(r.get("volume")), "amount": ms._fnum(r.get("amount")),
            "extra": {"api": "index_daily"},
        })
    return out


def ensure_bench_daily(conn, cfg, start, end):
    """确保基准指数日线覆盖 [start,end]；EM 优先、新浪回退。"""
    bench = cfg.get("benchmark") or {}
    symbol = bench.get("symbol") or "000300.SH"
    source = bench.get("source") or "akshare_index"
    row = conn.execute(
        "SELECT MAX(trade_date) AS m FROM stock_daily WHERE symbol=? AND source=?",
        (symbol, source)).fetchone()
    if row and row["m"] and row["m"] >= end:
        return 0
    err = None
    try:  # EM 指数日线
        import akshare as ak
        df = ak.stock_zh_index_daily_em(symbol=_ak_index_suffix(symbol))
        rows = _bench_rows_from_df(df, symbol, source, start, end)
        if not rows:
            raise RuntimeError("EM 返回空")
    except Exception as e:  # noqa: BLE001
        err = e
        rows = []
    if not rows:  # 新浪指数日线
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol=_ak_index_suffix(symbol))
            rows = _bench_rows_from_df(df, symbol, source, start, end)
        except Exception as e:  # noqa: BLE001
            err = e
    if not rows:
        log(f" 基准 {symbol} 日线拉取失败: {err or '返回空'}")
        return -1
    n = db.upsert_daily(conn, rows)
    log(f" 基准 {symbol} 日线补齐 {n} 条（{start}~{end}）")
    return n


def _sina_daily_rows(symbol, start, end):
    """新浪日线备用通道（EM hist 不可达时）→ 与 akshare 相同口径行。"""
    import akshare as ak
    code6 = symbol.split(".")[0]
    exch = symbol.rsplit(".", 1)[-1].lower()
    prefix = {"sh": "sh", "sz": "sz", "bj": "bj"}.get(exch, "sh")
    df = ak.stock_zh_a_daily(symbol=f"{prefix}{code6}",
                             start_date=start.replace("-", ""),
                             end_date=end.replace("-", ""),
                             adjust="qfq")
    rows = []
    for _, r in df.iterrows():
        d = str(r.get("date") or "")[:10]
        if not d:
            continue
        rows.append({
            "symbol": symbol, "source": "akshare", "trade_date": d,
            "open": ms._fnum(r.get("open")), "high": ms._fnum(r.get("high")),
            "low": ms._fnum(r.get("low")), "close": ms._fnum(r.get("close")),
            "volume": ms._fnum(r.get("volume")), "amount": ms._fnum(r.get("amount")),
            "adj_close": ms._fnum(r.get("adj_close")),
            "extra": {"api": "stock_zh_a_daily"},
        })
    return rows


def ensure_symbol_daily(conn, symbol, start, end):
    """确保单只 A 股日线覆盖 [start,end]，缺则补拉；0=无需拉，>0 补齐，-1 失败。"""
    row = conn.execute(
        "SELECT MAX(trade_date) AS m FROM stock_daily WHERE symbol=? AND source='akshare'",
        (symbol,)).fetchone()
    if row and row["m"] and row["m"] >= end:
        return 0
    err = None
    try:
        dfx = _data_fetcher()
        rows = dfx.fetch_akshare_daily(symbol, start, end)
    except Exception as e:  # noqa: BLE001
        rows, err = [], e
    if rows:
        return db.upsert_daily(conn, rows)
    try:  # 回退新浪日线
        rows = _sina_daily_rows(symbol, start, end)
    except Exception as e:  # noqa: BLE001
        err = e
    if not rows:
        log(f" {symbol} 日线拉取失败: {err or '返回空'}")
        return -1
    return db.upsert_daily(conn, rows)


def _close_map(conn, symbol, source, start, end):
    rows = conn.execute(
        "SELECT trade_date, close FROM stock_daily WHERE symbol=? AND source=? "
        "AND trade_date BETWEEN ? AND ? AND close IS NOT NULL",
        (symbol, source, start, end)).fetchall()
    return {r["trade_date"]: r["close"] for r in rows}

# 3) 结算到期的信号
def compute_pending(conn, cfg=None, limit=None):
    """结算可到期信号：pending/nodata → filled（或转 nodata/error 定格）。"""
    from bisect import bisect_left
    cfg = cfg or CONFIG
    sig = cfg.get("signal") or {}
    limit = int(limit or sig.get("compute_limit_per_run") or 120)
    bench = cfg.get("benchmark") or {}
    bench_symbol = bench.get("symbol") or "000300.SH"
    bench_source = bench.get("source") or "akshare_index"

    pend = db.pending_signals(conn, limit=limit)
    if not pend:
        return {"pending": 0, "filled": 0, "nodata": 0, "error": 0,
                "remain": 0}
    today = now_local(cfg).strftime("%Y-%m-%d")
    cal = ms.load_trade_days()
    hist = [d for d in cal if d <= today]
    if not hist:
        log(" 交易日历为空，compute 跳过")
        return {"pending": len(pend), "filled": 0, "nodata": 0,
                "error": 0, "remain": len(pend)}
    end = hist[-1]

    def _entry_day(sd, after):
        i = bisect_left(hist, sd)
        if after and i < len(hist) and hist[i] == sd:
            i += 1
        return i

    settle = []
    need_syms = set()
    min_sig = today
    for rec in pend:
        sd = rec["signal_date"]
        after = False
        t = (rec.get("signal_time") or "").strip()
        if t:
            try:
                hh, mm = [int(x) for x in t.split(":")[:2]]
                after = hh * 60 + mm > 15 * 60  # 15:00 收盘后发布 → 次日建仓
            except (TypeError, ValueError):
                pass
        i = _entry_day(sd, after)
        horizon = int(rec["horizon_days"])
        if i >= len(hist) or i + horizon >= len(hist):
            continue  # 建仓日或平仓日未到，继续 pending（无需拉行情）
        settle.append((rec, hist[i], hist[i + horizon]))
        need_syms.add(rec["symbol"])
        min_sig = min(min_sig, sd)
    if not settle:
        remain = conn.execute(
            "SELECT COUNT(*) AS n FROM signal_outcomes "
            "WHERE status IN ('pending','nodata')").fetchone()["n"]
        log(f" compute：今日无到期行（pending={len(pend)}），"
            f"仍待结算={remain}，不拉行情")
        return {"pending": len(pend), "filled": 0, "nodata": 0,
                "error": 0, "remain": remain}
    start = (datetime.strptime(min_sig, "%Y-%m-%d") -
             timedelta(days=25)).strftime("%Y-%m-%d")

    # pass2 行情保障：只拉今日可结算涉及的标的 + 基准
    syms = sorted(need_syms)
    for sym in syms:
        n = ensure_symbol_daily(conn, sym, start, end)
        if n:
            time.sleep(0.2)
    ensure_bench_daily(conn, cfg, start, end)

    marks = ",".join("?" * len(syms))
    rows = conn.execute(
        "SELECT symbol, trade_date, close FROM stock_daily "
        f"WHERE source='akshare' AND symbol IN ({marks}) "
        "AND close IS NOT NULL AND trade_date BETWEEN ? AND ?",
        (*syms, start, end)).fetchall()
    maps = {}
    for r in rows:
        maps.setdefault(r["symbol"], {})[r["trade_date"]] = r["close"]
    bench_map = {r["trade_date"]: r["close"] for r in conn.execute(
        "SELECT trade_date, close FROM stock_daily WHERE symbol=? AND source=? "
        "AND close IS NOT NULL AND trade_date BETWEEN ? AND ?",
        (bench_symbol, bench_source, start, end)).fetchall()}

    filled = nodata = errors = 0
    updates = []
    for rec, entry_date, exit_date in settle:
        m = maps.get(rec["symbol"], {})
        e_close, x_close = m.get(entry_date), m.get(exit_date)
        base = {
            "news_id": rec["news_id"], "symbol": rec["symbol"],
            "horizon_days": int(rec["horizon_days"]),
            "signal_date": rec["signal_date"],
            "signal_time": rec.get("signal_time") or "",
            "direction": rec["direction"], "bench_symbol": bench_symbol,
            "entry_date": entry_date, "exit_date": exit_date,
        }
        if e_close is None or x_close is None:
            base.update(status="error",
                        note="区间缺行情（停牌/退市/未上市），数据补齐后可手动重算")
            errors += 1
        else:
            be, bx = bench_map.get(entry_date), bench_map.get(exit_date)
            if be is None or bx is None:
                base.update(status="nodata", note="基准行情缺失，稍后自动重试")
                nodata += 1
            else:
                sym_pct = (x_close / e_close - 1.0) * 100.0
                bench_pct = (bx / be - 1.0) * 100.0
                base.update(status="filled", entry_price=e_close,
                            exit_price=x_close,
                            symbol_pct=round(sym_pct, 4),
                            bench_pct=round(bench_pct, 4),
                            excess_pct=round(sym_pct - bench_pct, 4))
                filled += 1
        updates.append(base)
    db.upsert_signal_rows(conn, updates)  # 单事务批量写回结算结果

    remain = conn.execute(
        "SELECT COUNT(*) AS n FROM signal_outcomes "
        "WHERE status IN ('pending','nodata')").fetchone()["n"]
    log(f" compute：filled={filled} nodata={nodata} error={errors} "
        f"仍待结算={remain}")
    return {"pending": len(pend), "filled": filled, "nodata": nodata,
            "error": errors, "remain": remain}

def build_report(conn, cfg=None, days=None):
    """近 N 天已结算信号 → JSON+Markdown 面板，写 data/reports。"""
    cfg = cfg or CONFIG
    rpt = cfg.get("reports") or {}
    days = int(days or rpt.get("days") or 90)
    today = now_local(cfg)
    cutoff = (today - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = conn.execute(
        "SELECT direction, horizon_days, symbol_pct, excess_pct, signal_date "
        "FROM signal_outcomes WHERE status='filled' AND signal_date>=? "
        "ORDER BY signal_date, id", (cutoff,)).fetchall()
    rows = [dict(r) for r in rows]
    groups = {}
    for r in rows:
        key = (r["direction"], int(r["horizon_days"]))
        groups.setdefault(key, []).append(r)

    panel = []
    for (direction, h), rs in sorted(groups.items(), key=lambda kv: kv[0][1]):
        n = len(rs)
        if direction == "up":
            hits = sum(1 for r in rs if (r["symbol_pct"] or 0) > 0)
        elif direction == "down":
            hits = sum(1 for r in rs if (r["symbol_pct"] or 0) < 0)
        else:
            hits = None
        syms = [r["symbol_pct"] or 0 for r in rs]
        exes = [r["excess_pct"] or 0 for r in rs]
        avg_sym = sum(syms) / n
        avg_ex = sum(exes) / n
        beat = sum(1 for e in exes if e > 0)
        panel.append({
            "direction": direction, "horizon_days": h, "n": n,
            "hits": hits,
            "hit_rate": round(hits / n, 4) if hits is not None else None,
            "avg_symbol_pct": round(avg_sym, 4),
            "avg_excess_pct": round(avg_ex, 4),
            "beat_bench_rate": round(beat / n, 4),
        })

    payload = {
        "generated_at": today.strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": days, "cutoff": cutoff,
        "total_filled": len(rows),
        "benchmark": (cfg.get("benchmark") or {}).get("symbol"),
        "rows": panel,
    }

    if os.environ.get("SIGNAL_REPORTS_DIR"):
        out_dir = Path(os.environ["SIGNAL_REPORTS_DIR"])
    else:
        cfg_dir = str(rpt.get("dir") or "")
        out_dir = Path(cfg_dir) if cfg_dir and cfg_dir != "data/reports" \
            else Path(db.storage_resolve(
                "SIGNAL_REPORTS_DIR", ("报告",),
                os.path.join("data", "reports")))
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"signal_eval_{today:%Y%m%d}"
    (out_dir / f"{base}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = _report_markdown(payload, cfg)
    (out_dir / f"{base}.md").write_text(md, encoding="utf-8")
    (out_dir / "signal_eval_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "signal_eval_latest.md").write_text(md, encoding="utf-8")
    log(f" 回测面板已更新：{out_dir / base}.md（filled={payload['total_filled']}）")
    return payload


def _report_markdown(payload, cfg=None):
    cfg = cfg or CONFIG
    lines = [
        f"# 新闻信号回测面板（近 {payload['window_days']} 天）",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 已结算信号：{payload['total_filled']} 条",
        f"- 基准：{payload['benchmark']}（同区间收盘涨跌幅）",
        "",
        "## 分方向 × 持有期",
        "",
        "| 方向 | 持有(交易日) | 样本 | 命中率 | 平均涨跌% | 超额均值% | 跑赢基准占比 |",
        "|------|------------|-----|-------|----------|-----------|-------------|",
    ]
    for r in payload["rows"]:
        hr = f"{r['hit_rate']*100:.1f}%" if r["hit_rate"] is not None else "—"
        lines.append(
            f"| {r['direction']} | {r['horizon_days']} | {r['n']} | "
            f"{hr} | {r['avg_symbol_pct']:+.2f} | "
            f"{r['avg_excess_pct']:+.2f} | "
            f"{r['beat_bench_rate']*100:.1f}% |")
    lines.append("")
    lines.append("> 方向口径：目前为标题利好/利空词表规则基线"
                 "（direction_method=lexicon），待接 Dify 中性语气模型后升级。")
    lines.append("")
    return "\n".join(lines)

# 5) evening 收尾一键 / CLI
def evening_stage(cfg=None, publish_date=None):
    """build → compute → report。由 news_scheduler 在 evening 窗口后调用。"""
    cfg = cfg or CONFIG
    conn = get_conn()
    try:
        built = build_signals(conn, cfg, publish_date=publish_date)
        comp = compute_pending(conn, cfg)
        report = None
        if (cfg.get("hooks") or {}).get("auto_report", True):
            report = build_report(conn, cfg)
    finally:
        conn.close()
    return {"built": built, "compute": comp,
            "report_total_filled": (report or {}).get("total_filled")}


def _cmd_status(conn):
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM signal_outcomes "
        "GROUP BY status ORDER BY status").fetchall()
    print("按状态:")
    for r in rows:
        print(f"  {r['status']}: {r['n']}")
    rows2 = conn.execute(
        "SELECT direction, horizon_days, COUNT(*) AS n FROM signal_outcomes "
        "WHERE status='filled' GROUP BY direction, horizon_days "
        "ORDER BY horizon_days, direction").fetchall()
    print("已结算分布:")
    for r in rows2:
        print(f"  {r['direction']:<8} h{r['horizon_days']:<3} n={r['n']}")


def main(argv=None):
    cfg = CONFIG
    p = argparse.ArgumentParser(description="新闻信号回测评估")
    p.add_argument("--build", action="store_true", help="建当日信号行")
    p.add_argument("--build-all", action="store_true", help="为全部已关联新闻建信号")
    p.add_argument("--compute", action="store_true", help="结算到期信号")
    p.add_argument("--report", action="store_true", help="刷新回测面板")
    p.add_argument("--evening", action="store_true", help="build+compute+report")
    p.add_argument("--status", action="store_true", help="流水线统计")
    p.add_argument("--date", default=None, help="YYYY-MM-DD")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)

    db.ensure_market_schema(get_db_path())
    conn = get_conn()
    try:
        if args.status:
            _cmd_status(conn)
            return 0
        if args.evening:
            res = evening_stage(cfg, publish_date=args.date)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if args.build or args.build_all:
            n = build_signals(conn, cfg,
                              publish_date=None if args.build_all else args.date)
            print(f"新增信号: {n}")
            return 0
        if args.compute:
            print(json.dumps(compute_pending(conn, cfg, limit=args.limit),
                             ensure_ascii=False))
            return 0
        if args.report:
            payload = build_report(conn, cfg, days=args.days)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        p.print_help()
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

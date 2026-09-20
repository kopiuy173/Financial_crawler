#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""market_snapshot.py — 需求 2：A股 全市场指数 + 板块 定时快照"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

_PROJ = Path(__file__).resolve().parent
os.chdir(_PROJ)

import yaml  # noqa: E402
import database as db  # noqa: E402

CONFIG_PATH = Path(os.environ.get("MARKET_CONFIG", _PROJ / "config" / "market.yaml"))
CACHE_DIR = Path(db.storage_resolve(
    "MARKET_CACHE_DIR", ("缓存与状态", "cache"), str(_PROJ / "data" / "cache")))
MARKET_LOG = _PROJ / "logs" / "market.log"
STATE_PATH = _PROJ / "logs" / "market_state.json"

_EM_INDEX_RAW = {
    "000001": "000001.SH", "000300": "000300.SH", "000688": "000688.SH",
    "000905": "000905.SH", "399001": "399001.SZ", "399006": "399006.SZ",
    "899050": "899050.BJ",
}


# 基础工具
def now_local(cfg=None):
    tz_name = (cfg or {}).get("timezone") or "Asia/Shanghai"
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.now()


def today_str(cfg=None):
    return now_local(cfg).strftime("%Y-%m-%d")


def log(msg):
    line = f"[{now_local():%F %T}] {msg}"
    MARKET_LOG.parent.mkdir(exist_ok=True)
    with open(MARKET_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def load_config(path=CONFIG_PATH):
    """读取 market.yaml；文件缺失/损坏时返回空配置（各功能自动禁用）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def get_db_path():
    return os.environ.get("KB_DB_PATH") or db.get_db_path()


def get_conn(db_path=None):
    return db.get_connection(db_path or get_db_path())


# 交易日历（akshare 官方日历，文件缓存）
def _calendar_path():
    return CACHE_DIR / "trade_days.json"


def load_trade_days(force_refresh=False, days_back=0):
    """返回升序交易日期列表（YYYY-MM-DD，含未来，方便算建仓/持有日）。

    缓存 >2 天自动刷新；网络失败时回退旧缓存。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = _calendar_path()
    data = None
    if cache.exists() and not force_refresh:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
    stale = not data or (datetime.now() - datetime.fromisoformat(
        data.get("updated"))).days > 2
    if stale or force_refresh:
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            dates = sorted(str(x)[:10] for x in df["trade_date"].tolist())
            data = {"updated": datetime.now().isoformat(timespec="seconds"),
                    "dates": dates}
            cache.write_text(json.dumps(data, ensure_ascii=False),
                             encoding="utf-8")
            log(f" 交易日历已刷新：{len(dates)} 天")
        except Exception as e:  # noqa: BLE001
            log(f" 交易日历刷新失败，使用旧缓存: {e}")
            if data is None:
                data = {"updated": datetime.now().isoformat(timespec="seconds"),
                        "dates": []}
    return data.get("dates") or []


def is_trade_day(dstr):
    """周末必非交易日；缓存日历命中为准，否则先按工作日放行。"""
    try:
        dt = datetime.strptime(dstr, "%Y-%m-%d")
    except ValueError:
        return False
    if dt.weekday() >= 5:
        return False
    dates = load_trade_days()
    return dstr in dates if dates else True


def trading_days_between(start, end):
    """闭区间 [start,end] 内的交易日（YYYY-MM-DD 升序）。"""
    dates = load_trade_days()
    return [d for d in dates if start <= d <= end] if dates else []


def next_trade_day(dstr, strict=False):
    """dstr 当日/之后第一个交易日（strict=True 则严格次日）。"""
    dates = load_trade_days()
    if not dates:
        return None
    if strict:
        target = [d for d in dates if d > dstr]
    else:
        target = [d for d in dates if d >= dstr]
    return target[0] if target else None

# 快照数据抓取（akshare 免费接口）
def _fnum(v):
    """宽松数字解析：容忍 %、逗号、--、nan。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "None", "nan", "NaN"):
        return None
    try:
        f = float(s)
        return f if f == f else None  # noqa: PLR0124 过滤 nan
    except (TypeError, ValueError):
        return None


def _index_sina_rows(cfg):
    """新浪指数 spot → 统一行列表（仅保留清单中的指数；主源，稳定可回退）。"""
    import akshare as ak
    want = {c["code"].split(".")[0].zfill(6): c for c in cfg.get("indices", [])}
    df = ak.stock_zh_index_spot_sina()
    rows = []
    for _, r in df.iterrows():
        raw = str(r.get("代码") or "")
        code6 = "".join(ch for ch in raw if ch.isdigit())[-6:]
        if code6 not in want:
            continue
        info = want[code6]
        rows.append({
            "code": info["code"],
            "name": info.get("name") or str(r.get("名称") or ""),
            "open": _fnum(r.get("今开")),
            "high": _fnum(r.get("最高")),
            "low": _fnum(r.get("最低")),
            "close": _fnum(r.get("最新价")),
            "pct_chg": _fnum(r.get("涨跌幅")),
            "volume": _fnum(r.get("成交量")),
            "amount": _fnum(r.get("成交额")),
            "extra": {"api": "stock_zh_index_spot_sina", "raw_code": raw},
        })
    return rows


def fetch_index_spot(cfg):
    """指数快照：优先新浪，失败或缺行时回退东财 EM。"""
    try:
        rows = _index_sina_rows(cfg)
        if rows:
            return rows
        log("  index sina 返回空，回退东财 EM")
    except Exception as e:  # noqa: BLE001
        log(f"  index sina 抓取失败，回退东财 EM: {e}")
    return _index_em_rows(cfg)


def _index_em_rows(cfg):
    """东财 EM 指数 spot → 统一行列表（备用通道）。"""
    import akshare as ak
    want = {c["code"].split(".")[0].zfill(6): c for c in cfg.get("indices", [])}
    df = ak.stock_zh_index_spot_em()
    rows = []
    for _, r in df.iterrows():
        raw = str(r.get("代码") or "")
        code6 = "".join(ch for ch in raw if ch.isdigit())[-6:]
        if code6 not in want:
            continue
        info = want[code6]
        rows.append({
            "code": info["code"],
            "name": info.get("name") or str(r.get("名称") or ""),
            "open": _fnum(r.get("今开")),
            "high": _fnum(r.get("最高")),
            "low": _fnum(r.get("最低")),
            "close": _fnum(r.get("最新价")),
            "pct_chg": _fnum(r.get("涨跌幅")),
            "volume": _fnum(r.get("成交量")),
            "amount": _fnum(r.get("成交额")),
            "extra": {"api": "stock_zh_index_spot_em", "raw_code": raw},
        })
    return rows


def _em_board_rows(api_name):
    """东财板块整表 → 行列表（code 优先板块代码列，缺失用板块名称）。"""
    import akshare as ak
    fn = getattr(ak, api_name)
    df = fn()
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        name = str(r.get("板块名称") or "")
        if not name:
            continue
        code = str(r.get("板块代码") or "").strip() or name
        rows.append({
            "code": code,
            "name": name,
            "close": _fnum(r.get("最新价")),
            "pct_chg": _fnum(r.get("涨跌幅")),
            "extra": {"api": api_name,
                      "up": _fnum(r.get("上涨家数")),
                      "down": _fnum(r.get("下跌家数")),
                      "leader": str(r.get("领涨股票") or "")},
        })
    return rows


def _sina_board_rows(indicator):
    """新浪板块整表（行业/概念）→ 统一行列表。"""
    import akshare as ak
    df = ak.stock_sector_spot(indicator=indicator)
    rows = []
    for _, r in df.iterrows():
        name = str(r.get("板块") or "")
        if not name:
            continue
        code = str(r.get("label") or "").strip() or name
        rows.append({
            "code": code,
            "name": name,
            "close": _fnum(r.get("平均价格")),
            "pct_chg": _fnum(r.get("涨跌幅")),
            "volume": _fnum(r.get("总成交量")),
            "amount": _fnum(r.get("总成交额")),
            "extra": {"api": "stock_sector_spot", "indicator": indicator,
                      "members": _fnum(r.get("公司家数")),
                      "leader": str(r.get("股票名称") or ""),
                      "leader_pct": _fnum(r.get("个股-涨跌幅"))},
        })
    return rows


def _boards_fallback(which):
    """板块快照：新浪 行业/概念 优先，失败或空则回退东财 EM 板块整表。"""
    if which == "industry_board":
        sina_indicator, em_api = "新浪行业", "stock_board_industry_name_em"
    else:
        sina_indicator, em_api = "概念", "stock_board_concept_name_em"
    try:
        rows = _sina_board_rows(sina_indicator)
        if rows:
            return rows
        log(f"  {which} sina 返回空，回退东财 EM")
    except Exception as e:  # noqa: BLE001
        log(f"  {which} sina 抓取失败，回退东财 EM: {e}")
    return _em_board_rows(em_api)


def fetch_kind_rows(kind):
    """按类别抓取当前快照行。kind: index / industry_board / concept_board。"""
    if kind == "index":
        return fetch_index_spot(load_config())
    if kind in ("industry_board", "concept_board"):
        return _boards_fallback(kind)
    raise ValueError(f"未知快照类别: {kind}")


def run_snapshot(conn, cfg, slot, trade_date=None, kinds=None):
    """抓取并落库一个时点快照。返回 {total, by_kind}；全空视为休市。"""
    trade_date = trade_date or today_str(cfg)
    kinds = kinds or [k for k in cfg.get("snapshot_kinds", ["index"])
                      if isinstance(k, str)]
    by_kind, total = {}, 0
    for kind in kinds:
        try:
            rows = fetch_kind_rows(kind)
        except Exception as e:  # noqa: BLE001
            log(f"  快照 {slot} {kind} 抓取失败: {e}")
            rows = []
        if not rows:
            by_kind[kind] = 0
            log(f"  快照 {slot} {kind}: 0 行（可能休市/接口限流）")
            continue
        rows = [{"trade_date": trade_date, "slot": slot, "kind": kind,
                 "code": r["code"], "name": r.get("name", ""),
                 "open": r.get("open"), "high": r.get("high"),
                 "low": r.get("low"), "close": r.get("close"),
                 "pct_chg": r.get("pct_chg"), "volume": r.get("volume"),
                 "amount": r.get("amount"), "extra": r.get("extra", {})}
                for r in rows]
        n = db.upsert_snapshot_rows(conn, rows)
        by_kind[kind] = n
        total += n
    return {"total": total, "by_kind": by_kind}

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"date": "", "slots": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(STATE_PATH)


def _slot_dt(cfg, slot, now):
    hh, mm = [int(x) for x in str(slot).split(":")[:2]]
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


_TERMINAL = ("done", "missed", "skipped")


def _due_points(cfg, now, date, state):
    """当前时刻真正“到期且未终态”的时点；空列表表示可以整体空转跳过。"""
    due = []
    slots = state.get("slots") or {}
    for point in cfg.get("snapshot_points", []):
        slot = str(point.get("time") or "09:25")
        rec = slots.get(slot) or {}
        if rec.get("date") == date and rec.get("status") in _TERMINAL:
            continue
        if not now >= _slot_dt(cfg, slot, now):
            continue
        due.append(slot)
    return due


def tick(cfg=None, conn=None, state=None):
    """执行所有“到期且允许”的快照；返回本次动作摘要列表。

    - 库内已有该 slot 数据 → done（幂等）
    - 迟到超过 allow_late_minutes → missed（盘中时点无法事后重建）
    - 抓取无数据（休市/限流）→ skipped，避免每 30 秒反复试
    - 接口失败 3 分钟冷却重试，直到宽限期结束

    优化：守护每 30s 轮询的主路径（无到期时点）不打开数据库、不写状态文件。
    """
    cfg = cfg or load_config()
    state = state if state is not None else load_state()
    now = now_local(cfg)
    date = now.strftime("%Y-%m-%d")
    actions = []
    if not _due_points(cfg, now, date, state):
        return actions
    # 即退化为原行为，不会因日历缺失而漏抓。
    if not is_trade_day(date):
        if state.get("date") != date:
            state["date"] = date
            state["slots"] = {}
        slots = state.setdefault("slots", {})
        for point in cfg.get("snapshot_points", []):
            slot = str(point.get("time") or "09:25")
            rec = slots.setdefault(slot, {"date": date})
            if rec.get("status") in _TERMINAL:
                continue
            rec.update(status="skipped", reason="non_trading_day")
            actions.append({"slot": slot, "action": "skipped",
                            "detail": "non_trading_day"})
        save_state(state)
        log(f" 非交易日 {date}，跳过全部行情快照（不入库）")
        return actions
    conn = conn or get_conn()
    try:
        if state.get("date") != date:
            state["date"] = date
            state["slots"] = {}
        for point in cfg.get("snapshot_points", []):
            slot = str(point.get("time") or "09:25")
            rec = state["slots"].setdefault(slot, {"date": date})
            if rec.get("date") != date:
                rec = state["slots"][slot] = {"date": date}
            if rec.get("status") in ("done", "missed", "skipped"):
                continue
            if not now >= _slot_dt(cfg, slot, now):
                continue
            if db.snapshot_done(conn, date, slot):
                rec.update(status="done")
                actions.append({"slot": slot, "action": "done"})
                continue
            late_min = (now - _slot_dt(cfg, slot, now)).total_seconds() / 60.0
            allow = float(point.get("allow_late_minutes") or 10)
            if late_min > allow:
                rec.update(status="missed", late_min=round(late_min, 1))
                log(f" 快照 {date} {slot} 迟到 {late_min:.0f} 分钟 > "
                    f"宽限 {allow}，标记 missed（盘中时点无法事后重建）")
                actions.append({"slot": slot, "action": "missed"})
                continue
            last = rec.get("last_attempt")
            if last and (now - datetime.fromisoformat(last)).total_seconds() < 180:
                continue  # 失败后 3 分钟冷却
            rec["last_attempt"] = now.isoformat(timespec="seconds")
            log(f" 快照 {date} {slot} 开始抓取…")
            res = run_snapshot(conn, cfg, slot, trade_date=date)
            if res["total"] > 0:
                rec.update(status="done", rows=res["total"])
                log(f" 快照 {date} {slot} 完成：{res['total']} 行"
                    f"（{res['by_kind']}）")
                actions.append({"slot": slot, "action": "snapshot",
                                "rows": res["total"]})
            else:
                rec.update(status="skipped")
                log(f" 快照 {date} {slot} 无数据，标记 skipped（疑似休市）")
                actions.append({"slot": slot, "action": "skipped"})
    finally:
        save_state(state)
        conn.close()
    return actions


def snapshot_status(cfg=None):
    """打印今日各时点行数 + 最近记录（CLI --status / 手工体检用）。"""
    cfg = cfg or load_config()
    conn = get_conn()
    date = today_str(cfg)
    lines = []
    for point in cfg.get("snapshot_points", []):
        slot = point["time"]
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM market_snapshot "
            "WHERE trade_date=? AND slot=?", (date, slot)).fetchone()["n"]
        lines.append(f"  {date} {slot} [{point.get('label','')}] 库内行数: {n}")
    recent = conn.execute(
        "SELECT trade_date, slot, kind, COUNT(*) AS n FROM market_snapshot "
        "GROUP BY trade_date, slot, kind ORDER BY trade_date DESC "
        "LIMIT 24").fetchall()
    conn.close()
    lines.append("  最近记录:")
    lines += [f"    {r['trade_date']} {r['slot']} {r['kind']}: {r['n']}"
              for r in recent]
    return "\n".join(lines)


# CLI
def main(argv=None):
    cfg = load_config()
    p = argparse.ArgumentParser(description="A股指数/板块定时快照")
    p.add_argument("--status", action="store_true", help="打印快照状态")
    p.add_argument("--run", action="store_true",
                   help="立即跑今天到期且宽限内未跑的快照")
    p.add_argument("--slot", default=None,
                   help="指定时点 09:25/11:30/15:00（缺省自动逐点）")
    p.add_argument("--force", action="store_true",
                   help="无视宽限期强制抓（仍要求已到时间点）")
    args = p.parse_args(argv)

    if args.status:
        print(snapshot_status(cfg))
        return 0

    db.ensure_market_schema(get_db_path())
    date = today_str(cfg)
    if args.run or args.slot:
        if args.slot and args.force:
            conn = get_conn()
            now = now_local(cfg)
            if not now >= _slot_dt(cfg, args.slot, now):
                print(f" 时点 {args.slot} 尚未到，跳过（--force 只放宽限期、不提前）")
                conn.close()
                return 1
            res = run_snapshot(conn, cfg, args.slot, trade_date=date)
            conn.close()
            print(json.dumps(res, ensure_ascii=False))
            return 0
        acts = tick(cfg)
        print(json.dumps({"date": date, "actions": acts},
                         ensure_ascii=False, indent=2))
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

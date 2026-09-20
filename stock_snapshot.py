#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""stock_snapshot.py — 数据补齐：全市场 A 股个股快照（默认 15:00 每日一次）"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import database as db  # noqa: E402

EM_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com",
            "82.push2.eastmoney.com", "1.push2.eastmoney.com")
EM_PATH = "/api/qt/clist/get"
SINA_LIST = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData")
SINA_COUNT = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount")
SINA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://finance.sina.com.cn"}
EM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
              "Referer": "https://quote.eastmoney.com/"}
FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FIELDS = "f12,f14,f2,f3,f15,f16,f17,f18,f5,f6,f8,f9,f20,f21,f23"
SLOT_DEFAULT = "15:00"


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%F %T"), msg), flush=True)


def to_symbol(code6):
    c = str(code6)
    if c.startswith(("6", "9")):
        return c + ".SH"
    if c.startswith(("0", "3")):
        return c + ".SZ"
    if c.startswith(("4", "8")):
        return c + ".BJ"
    return c


def _fnum(v):
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _em_page(pn, pz, timeout=20):
    params = {"pn": pn, "pz": pz, "po": 1, "np": 1, "fltt": 2, "invt": 2,
              "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fid": "f3",
              "fs": FS_ALL_A, "fields": FIELDS}
    last = None
    for host in EM_HOSTS:
        try:
            r = requests.get("https://" + host + EM_PATH, params=params,
                             headers=EM_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
    raise last if last else RuntimeError("东财镜像全部失败")


def fetch_all_spot(pz=1000, timeout=20, retry=3, delay=0.6):
    """分页抓取全市场 A 股快照；返回 (rows, total)。"""
    rows, total, pn, per_page = [], None, 1, None
    while True:
        data = None
        for attempt in range(1, retry + 1):
            try:
                data = _em_page(pn, pz, timeout=timeout)
                break
            except Exception as e:  # noqa: BLE001
                log("第 %d 页第 %d 次失败: %s" % (pn, attempt, str(e)[:120]))
                time.sleep(delay * attempt)
        if not data:
            raise RuntimeError("东财接口连续失败（第 %d 页）" % pn)
        payload = data.get("data") or {}
        diff = payload.get("diff") or []
        if total is None:
            total = int(payload.get("total") or 0)
        if not diff:
            break
        for it in diff:
            code = str(it.get("f12") or "").strip()
            if not code:
                continue
            rows.append({
                "code": code, "symbol": to_symbol(code),
                "name": str(it.get("f14") or "").strip(),
                "close": _fnum(it.get("f2")), "pct_chg": _fnum(it.get("f3")),
                "high": _fnum(it.get("f15")), "low": _fnum(it.get("f16")),
                "open": _fnum(it.get("f17")), "prev_close": _fnum(it.get("f18")),
                "volume": _fnum(it.get("f5")), "amount": _fnum(it.get("f6")),
                "turnover": _fnum(it.get("f8")), "pe": _fnum(it.get("f9")),
                "total_cap": _fnum(it.get("f20")),
                "float_cap": _fnum(it.get("f21")), "pb": _fnum(it.get("f23")),
            })
        got = len(diff)
        if per_page is None:
            per_page = got
        if total and len(rows) >= total:
            break
        if got < per_page:
            break
        pn += 1
        time.sleep(0.3)
        if pn > 120:
            log("分页达到上限 120 页，提前结束（已取 %d 行）" % len(rows))
            break
    return rows, (total or len(rows))


def fetch_from_sina(timeout=20, max_pages=80, delay=0.15):
    """新浪全市场回退：分页 100 条，返回 (rows, total)。"""
    total = None
    try:
        r = requests.get(SINA_COUNT, params={"node": "hs_a"},
                         headers=SINA_HEADERS, timeout=timeout)
        total = int(str(r.text).strip().strip('"') or 0)
    except Exception as e:  # noqa: BLE001
        log("新浪总数接口失败: %s" % str(e)[:80])
    rows = []
    for page in range(1, max_pages + 1):
        try:
            r = requests.get(SINA_LIST, params={"page": page, "num": 100,
                                                "sort": "symbol", "asc": 1,
                                                "node": "hs_a"},
                             headers=SINA_HEADERS, timeout=timeout)
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log("新浪第 %d 页失败: %s" % (page, str(e)[:80]))
            break
        if not data:
            break
        for it in data:
            code = str(it.get("code") or "").strip()
            if not code:
                continue
            rows.append({
                "code": code, "symbol": to_symbol(code),
                "name": str(it.get("name") or "").strip(),
                "close": _fnum(it.get("trade")),
                "pct_chg": _fnum(it.get("changepercent")),
                "open": _fnum(it.get("open")), "high": _fnum(it.get("high")),
                "low": _fnum(it.get("low")),
                "prev_close": _fnum(it.get("settlement")),
                "volume": _fnum(it.get("volume")),
                "amount": _fnum(it.get("amount")),
                "turnover": _fnum(it.get("turnoverratio")),
                "total_cap": _fnum(it.get("mktcap")),
                "float_cap": _fnum(it.get("nmc")),
                "pe": _fnum(it.get("per")), "pb": _fnum(it.get("pb")),
            })
        if total and len(rows) >= total:
            break
        time.sleep(delay)
    return rows, (total or len(rows))


def load_cfg():
    try:
        import yaml
        cfg = yaml.safe_load((HERE / "config" / "market.yaml").read_text(
            encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    return (cfg.get("stock_snapshot") or {})


def _slot_dt(date_str, slot):
    hh, mm = [int(x) for x in str(slot).split(":")[:2]]
    return datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hh, minute=mm)


_LOGGED = {}


def _log_once(key, msg):
    """同一事件只记一次，避免守护每 30 秒轮询刷日志。"""
    if _LOGGED.get(key):
        return
    _LOGGED[key] = True
    log(msg)


def run(trade_date=None, force=False, cfg=None):
    """按时点抓取并写入；幂等（当日该时点已有数据即跳过）。"""
    cfg = cfg if cfg is not None else load_cfg()
    if not cfg.get("enabled", True) and not force:
        _log_once("off", "stock_snapshot.enabled=false，跳过")
        return {"action": "skipped"}
    now = datetime.now()
    trade_date = trade_date or now.strftime("%Y-%m-%d")
    slot = str(cfg.get("slot") or SLOT_DEFAULT)
    late = (now - _slot_dt(trade_date, slot)).total_seconds() / 60.0
    allow = float(cfg.get("allow_late_minutes") or 180)
    if late < 0 and not force:
        return {"action": "idle"}
    if not force:
        try:
            import market_snapshot as ms
            if not ms.is_trade_day(trade_date):
                _log_once("nontrade:" + trade_date,
                          "非交易日 %s，跳过个股快照" % trade_date)
                return {"action": "skipped", "detail": "non_trading_day"}
        except Exception as e:  # noqa: BLE001
            _log_once("cal:" + trade_date,
                      "交易日历校验失败（继续执行）: %s" % str(e)[:80])
    conn = db.get_connection()
    try:
        done = conn.execute(
            "SELECT COUNT(*) c FROM stock_snapshot WHERE trade_date=? AND slot=?",
            (trade_date, slot)).fetchone()["c"]
        if done and not force:
            _log_once("done:%s:%s" % (trade_date, slot),
                      "已完成：%s %s 共 %d 行" % (trade_date, slot, done))
            return {"action": "done", "rows": done}
        if late > allow and not force:
            _log_once("missed:%s:%s" % (trade_date, slot),
                      "迟到 %.0f 分钟 超过宽限 %.0f，标记 missed" % (late, allow))
            return {"action": "missed", "late_min": round(late, 1)}
        timeout = int(cfg.get("requests_timeout") or 20)
        src = "em"
        try:
            rows, total = fetch_all_spot(
                pz=int(cfg.get("page_size") or 100), timeout=timeout)
        except Exception as e:  # noqa: BLE001
            log("东财不可用（%s），切换新浪源" % str(e)[:100])
            rows, total = fetch_from_sina(timeout=timeout)
            src = "sina"
        if not rows:
            raise RuntimeError("两个数据源均未取到数据")
        for r in rows:
            r["trade_date"] = trade_date
            r["slot"] = slot
            r["source"] = src
        n = db.upsert_stock_snapshot_rows(conn, rows)
        log("个股快照完成：%s %s 写入 %d 行（接口 total=%s）"
            % (trade_date, slot, n, total))
        return {"action": "snapshot", "rows": n, "total": total}
    finally:
        conn.close()


def status():
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT trade_date, slot, COUNT(*) c FROM stock_snapshot "
            "GROUP BY trade_date, slot ORDER BY trade_date DESC LIMIT 10"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="全市场 A 股个股快照（默认 15:00）")
    p.add_argument("--run", action="store_true")
    p.add_argument("--date", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--status", action="store_true")
    args = p.parse_args(argv)
    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    res = run(trade_date=args.date, force=args.force)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())


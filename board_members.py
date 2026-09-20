#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""board_members.py — 数据补齐：板块成分（行业先行，概念可选）"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import database as db  # noqa: E402

EM_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com",
            "82.push2.eastmoney.com", "1.push2.eastmoney.com")
EM_PATH = "/api/qt/clist/get"
EM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
              "Referer": "https://quote.eastmoney.com/"}
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"


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


def load_cfg():
    try:
        import yaml
        cfg = yaml.safe_load((HERE / "config" / "market.yaml").read_text(
            encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    return (cfg.get("board_members") or {})


def latest_boards(conn, kinds, limit=0, date=None):
    """取各 kind 指定日期（缺省为最近交易日）15:00 的板块清单。"""
    out = []
    for kind in kinds:
        if date:
            d = date
        else:
            row = conn.execute(
                "SELECT MAX(trade_date) d FROM market_snapshot "
                "WHERE kind=? AND slot='15:00'", (kind,)).fetchone()
            d = row["d"] if row else None
        if not d:
            continue
        for r in conn.execute(
                "SELECT code, name FROM market_snapshot WHERE kind=? AND "
                "trade_date=? AND slot='15:00' ORDER BY code",
                (kind, d)).fetchall():
            out.append({"kind": kind, "code": r["code"], "name": r["name"],
                        "trade_date": d})
    return out[:limit] if limit and limit > 0 else out


SINA_LIST = ("https://vip.stock.finance.sina.com.cn/quotes_service/"
             "api/json_v2.php/Market_Center.getHQNodeData")
SINA_COUNT = ("https://vip.stock.finance.sina.com.cn/quotes_service/"
              "api/json_v2.php/Market_Center.getHQNodeStockCount")
SINA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://finance.sina.com.cn"}


def _sina_total(node, timeout=20):
    try:
        r = requests.get(SINA_COUNT, params={"node": node},
                         headers=SINA_HEADERS, timeout=timeout)
        return int(str(r.text).strip().strip('"') or 0)
    except Exception:  # noqa: BLE001
        return 0


def _fetch_members_sina(node, timeout=20, retry=3, delay=0.6, max_pages=40):
    """新浪成分：node 与 market_snapshot 的板块代码同源（new_* 与 gn_*）。"""
    total = _sina_total(node, timeout)
    out, per_page, last = [], None, None
    for page in range(1, max_pages + 1):
        data, got = None, None
        for attempt in range(1, retry + 1):
            try:
                r = requests.get(SINA_LIST,
                                 params={"page": page, "num": 100,
                                         "sort": "symbol", "asc": 1,
                                         "node": node},
                                 headers=SINA_HEADERS, timeout=timeout)
                data = r.json()
                got = len(data) if isinstance(data, list) else 0
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(delay * attempt)
        if got is None:
            raise RuntimeError("新浪成分抓取失败 %s: %s"
                               % (node, str(last)[:120]))
        if not data:
            break
        for it in data:
            code = str(it.get("code") or "").strip()
            if code:
                out.append({"symbol": to_symbol(code),
                            "stock_name": str(it.get("name") or "").strip()})
        if per_page is None:
            per_page = got
        if got < per_page:
            break
        if total and len(out) >= total:
            break
        time.sleep(delay)
    return out


def _fetch_members_em(board_code, timeout=20, retry=3, delay=0.6, max_pages=40):
    """分页抓取单个板块成分（b:BKxxxx）；镜像每页上限 100，按实际返回分页。"""
    out, per_page, pn, last = [], None, 1, None
    while pn <= max_pages:
        params = {"pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                  "ut": EM_UT, "fid": "f3", "fs": "b:" + str(board_code),
                  "fields": "f12,f14"}
        diff, got = None, None
        for attempt in range(1, retry + 1):
            for host in EM_HOSTS:
                try:
                    r = requests.get("https://" + host + EM_PATH,
                                     params=params, headers=EM_HEADERS,
                                     timeout=timeout)
                    r.raise_for_status()
                    diff = ((r.json().get("data") or {}).get("diff") or [])
                    got = len(diff)
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
            if got is not None:
                break
            time.sleep(delay * attempt)
        if got is None:
            raise RuntimeError("成分抓取失败 %s: %s"
                               % (board_code, str(last)[:120]))
        for it in diff:
            code = str(it.get("f12") or "").strip()
            if code:
                out.append({"symbol": to_symbol(code),
                            "stock_name": str(it.get("f14") or "").strip()})
        if per_page is None:
            per_page = got
        if got == 0 or got < per_page:
            break
        pn += 1
        time.sleep(delay)
    return out



def fetch_members(board_code, timeout=20, retry=3, delay=0.6, max_pages=40):
    """按前缀分流：BK 前缀走东财，其余（新浪风格 new_ 与 gn_）走新浪 node。"""
    code = str(board_code).strip()
    if code.upper().startswith("BK"):
        return _fetch_members_em(code, timeout=timeout, retry=retry,
                                 delay=delay, max_pages=max_pages)
    return _fetch_members_sina(code, timeout=timeout, retry=retry,
                               delay=delay, max_pages=max_pages)

def _fresh(conn, kind, code, refresh_days):
    if refresh_days <= 0:
        return False
    row = conn.execute(
        "SELECT MAX(updated_at) t FROM board_members WHERE board_kind=? AND "
        "board_code=?", (kind, code)).fetchone()
    t = row["t"] if row else None
    if not t:
        return False
    try:
        last = datetime.fromisoformat(t)
    except ValueError:
        return False
    return datetime.now() - last < timedelta(days=refresh_days)


def refresh(force=False, cfg=None, date=None, only_codes=None):
    """按板块清单刷新成分；相同板块在 refresh_days 内不重复抓取。"""
    cfg = cfg if cfg is not None else load_cfg()
    if not cfg.get("enabled", True) and not force:
        log("board_members.enabled=false，跳过")
        return {"action": "skipped"}
    kinds = list(cfg.get("kinds") or ["industry_board"])
    if cfg.get("include_concepts") and "concept_board" not in kinds:
        kinds.append("concept_board")
    limit = int(cfg.get("max_boards") or 0)
    refresh_days = int(cfg.get("refresh_days") or 0)
    delay = float(cfg.get("request_delay_seconds") or 0.4)
    conn = db.get_connection()
    try:
        if only_codes:
            keep = set(only_codes)
            d = date or conn.execute(
                "SELECT MAX(trade_date) d FROM market_snapshot "
                "WHERE slot='15:00'").fetchone()["d"]
            boards = []
            for r in conn.execute(
                    "SELECT DISTINCT kind, code, name FROM market_snapshot "
                    "WHERE trade_date=? AND slot='15:00'", (d,)).fetchall():
                if r["code"] in keep:
                    boards.append({"kind": r["kind"], "code": r["code"],
                                   "name": r["name"]})
        else:
            boards = latest_boards(conn, kinds, limit, date=date)
        if not boards:
            log("market_snapshot 中未找到板块清单，跳过")
            return {"action": "no_boards", "boards": 0}
        ok, skip, fail, members_total = 0, 0, [], 0
        for b in boards:
            if not force and _fresh(conn, b["kind"], b["code"], refresh_days):
                skip += 1
                continue
            try:
                members = fetch_members(b["code"])
            except Exception as e:  # noqa: BLE001
                failed_code = b["code"]
                fail.append(failed_code)
                log("板块 %s(%s) 成分失败: %s" % (b["name"], failed_code,
                                                 str(e)[:120]))
                continue
            if not members:
                fail.append(b["code"])
                continue
            conn.execute("DELETE FROM board_members WHERE board_kind=? AND "
                         "board_code=?", (b["kind"], b["code"]))
            rows = [{"board_kind": b["kind"], "board_code": b["code"],
                     "board_name": b["name"], **m} for m in members]
            members_total += db.upsert_board_members(conn, rows)
            ok += 1
            log("板块 %s(%s) 成分 %d 个" % (b["name"], b["code"], len(members)))
            time.sleep(delay)
        log("成分刷新完成：成功 %d，跳过 %d，失败 %d，写入 %d 行"
            % (ok, skip, len(fail), members_total))
        return {"action": "refresh", "boards": len(boards), "ok": ok,
                "skipped": skip, "failed": fail, "members": members_total}
    finally:
        conn.close()


def boards_from_predictions(conn, date):
    """当日预测主题对应的板块（按名称在 market_snapshot 中对名）。"""
    out = []
    for p in conn.execute(
            "SELECT kind, theme FROM predictions WHERE trade_date=? "
            "ORDER BY side, seq", (date,)).fetchall():
        row = conn.execute(
            "SELECT code, name FROM market_snapshot WHERE trade_date=? AND "
            "slot='15:00' AND kind=? AND name=?",
            (date, p["kind"], p["theme"])).fetchone()
        if row:
            out.append({"kind": p["kind"], "code": row["code"],
                        "name": row["name"]})
    return out


def ensure_predicted(date, cfg=None, force=True):
    """定向刷新：只抓当日预测主题对应板块的成分（每日，成本极低）。"""
    cfg = cfg if cfg is not None else load_cfg()
    conn = db.get_connection()
    try:
        boards = boards_from_predictions(conn, date)
    finally:
        conn.close()
    if not boards:
        log("当日无预测主题可定向刷新（%s）" % date)
        return {"action": "no_boards", "boards": 0}
    codes = [b["code"] for b in boards]
    log("定向刷新 %d 个预测主题板块：%s" % (len(codes), codes))
    res = refresh(force=force, cfg=cfg, date=date, only_codes=codes)
    res["predicted"] = codes
    return res


def ensure_fresh(cfg=None):
    """调度器入口：仅在超过 refresh_days 时刷新（失败不影响主链路）。"""
    try:
        return refresh(force=False, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        log("成分刷新异常: %s" % e)
        return {"action": "error", "detail": str(e)[:200]}


def status():
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT board_kind, COUNT(DISTINCT board_code) boards, "
            "COUNT(*) members, MAX(updated_at) last_at FROM board_members "
            "GROUP BY board_kind").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="板块成分刷新（行业先行）")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--only-predicted", action="store_true",
                   help="只刷新当日预测主题对应的板块")
    p.add_argument("--date", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--status", action="store_true")
    args = p.parse_args(argv)
    if args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if args.only_predicted:
        res = ensure_predicted(args.date or datetime.now().strftime("%Y-%m-%d"))
    else:
        res = refresh(force=args.force, date=args.date)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())


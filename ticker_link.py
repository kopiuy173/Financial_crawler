#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ticker_link.py — 需求 2：新闻→A股标的 关联（news_tickers）"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_PROJ = Path(__file__).resolve().parent
os.chdir(_PROJ)

import database as db  # noqa: E402
import market_snapshot as ms  # noqa: E402  （复用 log / 配置 / 时区）

CONFIG = ms.load_config()


def now_local(cfg=None):
    return ms.now_local(cfg or CONFIG)


def log(msg):
    ms.log(msg)


def get_db_path():
    return os.environ.get("KB_DB_PATH") or db.get_db_path()


def get_conn():
    return db.get_connection(get_db_path())

# A股 名称→代码 字典
def _exchange_of(code6):
    """6 位纯数字代码 → 交易所。只收 A 股（跳过 B 股 900/200 开头）。"""
    if code6[:2] in ("60", "68"):
        return "SH"
    if code6[:2] in ("00", "30"):
        return "SZ"
    if code6[0] in ("4", "8") or code6[:2] == "92":
        return "BJ"
    return ""


def _ak_universe_rows():
    """akshare A股代码名称表 → 统一行列表（NFKC 规范化名称）。"""
    import unicodedata
    import akshare as ak
    df = ak.stock_info_a_code_name()
    rows = []
    for _, r in df.iterrows():
        code6 = str(r.get("code") or r.get("代码") or "").strip()
        if not code6.isdigit():
            continue
        code6 = code6.zfill(6)
        exch = _exchange_of(code6)
        if not exch:
            continue
        name = unicodedata.normalize("NFKC",
                                     str(r.get("name") or r.get("名称") or ""))
        name = name.replace(" ", "").replace("\u3000", "").strip()
        if len(name) < 2 or name.endswith("退"):
            continue
        rows.append({"symbol": f"{code6}.{exch}", "name": name,
                     "exchange": exch, "market": "cn"})
    return rows


def ensure_universe(conn, cfg=None, force=False):
    """确保名称字典非空且不过期；过期时从 akshare 刷新。返回库内字典行数。"""
    cfg = cfg or CONFIG
    link_cfg = cfg.get("link") or {}
    if not force:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(updated_at) AS u FROM ticker_universe"
        ).fetchone()
        if row and row["n"]:
            refresh_days = int(link_cfg.get("universe_refresh_days") or 30)
            if row["u"]:
                age_days = (datetime.now() - datetime.fromisoformat(row["u"])).days
                if age_days < refresh_days:
                    return row["n"]
    try:
        rows = _ak_universe_rows()
    except Exception as e:  # noqa: BLE001
        log(f" A股字典刷新失败（用库内旧数据兜底）: {e}")
        return conn.execute(
            "SELECT COUNT(*) AS n FROM ticker_universe").fetchone()["n"]
    if not rows:
        log(" A股字典刷新返回空，保留旧数据")
        return conn.execute(
            "SELECT COUNT(*) AS n FROM ticker_universe").fetchone()["n"]
    n = db.upsert_ticker_universe(conn, rows)
    log(f" A股名称字典已刷新：{n} 个标的")
    return n


def _load_universe(conn):
    """返回 [(name, symbol)]，按名称长度降序。"""
    rows = conn.execute(
        "SELECT symbol, name FROM ticker_universe").fetchall()
    return sorted([(r["name"], r["symbol"]) for r in rows],
                  key=lambda x: len(x[0]), reverse=True)

def _norm_cn(s):
    """与字典同口径规范化（NFKC + 去空格）：doc 侧也要做，才能命中
    “万 科Ａ”这类全角/夹空格写法（字典名称入库前已 NFKC 化）。"""
    import unicodedata
    return unicodedata.normalize("NFKC", s).replace(" ", "").replace("\u3000", "")


# 单条新闻关联
def link_doc(conn, cfg, universe, doc):
    """为一条新闻匹配标的。返回 link dict 列表（已按标题优先排序截断）。"""
    link_cfg = cfg.get("link") or {}
    max_links = int(link_cfg.get("max_links_per_news") or 8)
    title = _norm_cn(str(doc.get("title") or ""))
    content = _norm_cn(str(doc.get("content") or ""))[:1200]
    hits = {}
    for name, symbol in universe:
        if len(name) < 2:
            continue
        if name in title:
            hit = hits.get(symbol)
            if not hit or hit["matched_in"] != "title":
                hits[symbol] = {"symbol": symbol, "name": name,
                                "matched_in": "title", "confidence": 1.0}
            continue
        if len(name) >= 3 and content and name in content:
            if symbol not in hits:
                hits[symbol] = {"symbol": symbol, "name": name,
                                "matched_in": "content", "confidence": 0.6}
    ranked = sorted(hits.values(),
                    key=lambda h: (h["matched_in"] == "title",
                                   len(h["name"])), reverse=True)
    links = []
    for h in ranked[:max_links]:
        links.append({
            "news_id": doc["id"], "symbol": h["symbol"], "name": h["name"],
            "method": "rules", "matched_in": h["matched_in"],
            "confidence": h["confidence"],
        })
    return links


def link_date(conn, cfg=None, publish_date=None, force=False):
    """为指定日期（缺省=所有）未关联的新闻补关联。返回统计。"""
    cfg = cfg or CONFIG
    if not (cfg.get("link") or {}).get("enabled", True):
        return {"docs": 0, "links": 0, "skipped": True}
    universe_n = ensure_universe(conn, cfg, force=force)
    universe = _load_universe(conn)
    if not universe:
        log(" 名称字典为空且无法刷新，本次跳过关联")
        return {"docs": 0, "links": 0, "universe": 0}
    docs = db.unlinked_news(conn, publish_date=publish_date, limit=5000)
    total_links = 0
    linked_docs = 0
    for doc in docs:
        links = link_doc(conn, cfg, universe, doc)
        if links:
            n = db.add_news_tickers(conn, links)
            total_links += n
            linked_docs += 1
    if publish_date:
        log(f" 标的关联 {publish_date}：{len(docs)} 条待关联新闻 → "
            f"{linked_docs} 条命中 / {total_links} 个关联")
    else:
        log(f" 标的关联：扫描 {len(docs)} 条未关联新闻 → "
            f"{linked_docs} 条命中 / {total_links} 个关联")
    return {"docs": len(docs), "linked_docs": linked_docs,
            "links": total_links, "universe": universe_n}


# CLI
def main(argv=None):
    cfg = CONFIG
    p = argparse.ArgumentParser(description="新闻→A股标的关联")
    p.add_argument("--link-date", default=None, help="YYYY-MM-DD，缺省全部未关联")
    p.add_argument("--force", action="store_true",
                   help="强制刷新 A股名称字典")
    p.add_argument("--refresh-universe", action="store_true",
                   help="仅刷新 A股名称字典")
    p.add_argument("--stats", action="store_true", help="关联统计")
    args = p.parse_args(argv)

    db.ensure_market_schema(get_db_path())
    conn = get_conn()
    try:
        if args.refresh_universe:
            n = ensure_universe(conn, cfg, force=True)
            print(f"A股名称字典共 {n} 个标的")
            return 0
        if args.stats:
            rows = conn.execute(
                "SELECT publish_date, COUNT(*) AS n FROM news_tickers t "
                "JOIN documents d ON d.id=t.news_id GROUP BY publish_date "
                "ORDER BY publish_date DESC LIMIT 15").fetchall()
            print("按日期关联数:")
            for r in rows:
                print(f"  {r['publish_date']}: {r['n']}")
            return 0
        res = link_date(conn, cfg, publish_date=args.link_date, force=args.force)
        print(f"完成: {res}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

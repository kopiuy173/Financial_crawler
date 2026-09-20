#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""data_tool_server.py — 金融数据查询工具（HTTP/OpenAPI 服务）"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_FILE = Path(__file__).resolve()
PY_DIR = _FILE.parent
MIG_DIR = PY_DIR.parent
PROJ_ROOT = MIG_DIR.parent
sys.path.insert(0, str(PROJ_ROOT))
os.chdir(PROJ_ROOT)

import database as db                      # noqa: E402

VERSION = "0.1.0"


def get_conn():
    return db.get_connection(os.environ.get("KB_DB_PATH") or db.get_db_path())


def qs(params, name, default=None):
    v = params.get(name, [default])[0]
    return default if v in (None, "") else v


def handle_daily(q):
    conn = get_conn()
    try:
        rows = db.query_daily(
            conn, symbol=qs(q, "symbol", ""), start=qs(q, "start"),
            end=qs(q, "end"), source=qs(q, "source"), limit=qs(q, "limit", 2000),
        )
    finally:
        conn.close()
    return {"data": rows, "count": len(rows)}


def handle_fundamental(q):
    symbol = qs(q, "symbol", "")
    if not symbol:
        return {"error": "symbol 必填"}
    conn = get_conn()
    try:
        row = db.latest_fundamental(conn, symbol, source=qs(q, "source"))
    finally:
        conn.close()
    return {"data": [row] if row else []}


def handle_financial(q):
    symbol = qs(q, "symbol", "")
    if not symbol:
        return {"error": "symbol 必填"}
    conn = get_conn()
    try:
        rows = db.latest_financials(conn, symbol, source=qs(q, "source"),
                                    periods=qs(q, "periods", 4))
    finally:
        conn.close()
    return {"data": rows, "count": len(rows)}


def handle_kb_docs(q):
    conn = get_conn()
    try:
        sql = "SELECT id, source_type, title, url, publish_date, tags FROM documents WHERE 1=1"
        params = []
        if qs(q, "source_type"):
            sql += " AND source_type=?"
            params.append(qs(q, "source_type"))
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(qs(q, "limit", 20)))
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return {"data": [dict(r) for r in rows]}


def handle_kb_search(q):
    """关键词检索文档正文（轻量 LIKE），供 Agent 找不到精确数据时兜底。"""
    kw = qs(q, "q", "")
    if not kw:
        return {"error": "q 必填"}
    conn = get_conn()
    try:
        like = f"%{kw}%"
        sql = ("SELECT d.id, d.source_type, d.title, d.url, d.publish_date, "
               "c.content FROM documents d JOIN document_chunks c ON c.document_id=d.id "
               "WHERE c.content LIKE ? OR d.title LIKE ? ORDER BY d.created_at DESC LIMIT ?")
        rows = conn.execute(sql, (like, like, int(qs(q, "limit", 10)))).fetchall()
    finally:
        conn.close()
    return {"data": [dict(r) for r in rows]}


def handle_market_snapshot(q):
    """行情快照：按日期/时段/类别查 指数+板块 快照（需求2）。"""
    conn = get_conn()
    try:
        sql = ("SELECT trade_date, slot, kind, code, name, close, pct_chg, "
               "volume, amount FROM market_snapshot WHERE 1=1")
        params = []
        for col in ("trade_date", "slot", "kind"):
            v = qs(q, col)
            if v:
                sql += f" AND {col}=?"
                params.append(v)
        sql += " ORDER BY trade_date DESC, slot DESC, kind, code LIMIT ?"
        params.append(int(qs(q, "limit", 500)))
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return {"data": [dict(r) for r in rows], "count": len(rows)}


def handle_news_tickers(q):
    """新闻→A股标的 关联明细（按日期过滤，需求2）。"""
    conn = get_conn()
    try:
        sql = ("SELECT d.id AS news_id, d.title, d.publish_date, t.symbol, "
               "t.name AS ticker_name, t.method, t.matched_in, t.confidence "
               "FROM news_tickers t JOIN documents d ON d.id=t.news_id "
               "WHERE 1=1")
        params = []
        if qs(q, "date"):
            sql += " AND d.publish_date=?"
            params.append(qs(q, "date"))
        if qs(q, "symbol"):
            sql += " AND t.symbol=?"
            params.append(qs(q, "symbol"))
        sql += " ORDER BY d.publish_date DESC, d.id LIMIT ?"
        params.append(int(qs(q, "limit", 200)))
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return {"data": [dict(r) for r in rows], "count": len(rows)}


def handle_signal_panel(q):
    """近 N 天信号回测面板（命中率/平均涨跌/超额，按方向×持有期）。"""
    from datetime import datetime, timedelta
    days = int(qs(q, "days", 90))
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT direction, horizon_days, symbol_pct, excess_pct "
            "FROM signal_outcomes WHERE status='filled' AND signal_date>=?",
            (cutoff,)).fetchall()
    finally:
        conn.close()
    groups = {}
    for r in rows:
        key = (r["direction"], int(r["horizon_days"]))
        groups.setdefault(key, []).append(r)
    panel = []
    for (direction, h), rs in groups.items():
        n = len(rs)
        if direction == "up":
            hits = sum(1 for x in rs if (x["symbol_pct"] or 0) > 0)
        elif direction == "down":
            hits = sum(1 for x in rs if (x["symbol_pct"] or 0) < 0)
        else:
            hits = None
        ex = [x["excess_pct"] or 0 for x in rs]
        panel.append({
            "direction": direction, "horizon_days": h, "n": n,
            "hits": hits,
            "hit_rate": round(hits / n, 4) if hits is not None else None,
            "avg_excess_pct": round(sum(ex) / n, 4),
        })
    panel.sort(key=lambda x: (x["horizon_days"], x["direction"]))
    return {"days": days, "cutoff": cutoff, "total_filled": len(rows),
            "rows": panel}


def build_openapi(host_header):
    """为 Dify 生成 OpenAPI 3.0 描述（servers 用请求 Host 动态拼接）。"""
    return {
        "openapi": "3.0.0",
        "info": {"title": "金融数据查询工具", "description":
                 "查询本地 SQLite 中股票日线、估值/财务与知识库文档；"
                 "symbol 示例: 600519.SH / AAPL / ^GSPC。日期格式 YYYY-MM-DD。",
                 "version": VERSION},
        "servers": [{"url": f"http://{host_header}"}],
        "paths": {
            "/stock/daily": {"get": {"operationId": "queryDaily",
                                     "summary": "查日线行情（可增量/区间）",
                                     "parameters": [
                {"name": "symbol", "in": "query", "required": True,
                 "schema": {"type": "string"}},
                {"name": "start", "in": "query", "schema": {"type": "string"}},
                {"name": "end", "in": "query", "schema": {"type": "string"}},
                {"name": "source", "in": "query", "schema": {"type": "string"}},
                {"name": "limit", "in": "query", "schema": {"type": "integer"}},
            ], "responses": {"200": {"description": "ok"}}}},
            "/stock/fundamental": {"get": {"operationId": "queryFundamental",
                "summary": "查最新估值快照(PE/PB/市值)", "parameters": [
                {"name": "symbol", "in": "query", "required": True,
                 "schema": {"type": "string"}},
                {"name": "source", "in": "query", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}}}},
            "/stock/financial": {"get": {"operationId": "queryFinancial",
                "summary": "查最近 N 期财务指标(ROE/毛利率/营收等)", "parameters": [
                {"name": "symbol", "in": "query", "required": True,
                 "schema": {"type": "string"}},
                {"name": "periods", "in": "query", "schema": {"type": "integer"}},
                {"name": "source", "in": "query", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}}}},
            "/kb/search": {"get": {"operationId": "searchKnowledge",
                "summary": "关键词检索知识库正文（LLM 兜底用）", "parameters": [
                {"name": "q", "in": "query", "required": True,
                 "schema": {"type": "string"}},
                {"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok"}}}},
            "/market/snapshot": {"get": {"operationId": "queryMarketSnapshot",
                "summary": "指数/板块定时快照（按日期/时段/类别过滤）",
                "parameters": [
                {"name": "trade_date", "in": "query", "schema": {"type": "string"}},
                {"name": "slot", "in": "query", "schema": {"type": "string"}},
                {"name": "kind", "in": "query", "schema": {"type": "string"}},
                {"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok"}}}},
            "/news/tickers": {"get": {"operationId": "queryNewsTickers",
                "summary": "新闻关联的 A 股标的（按日期/代码过滤）",
                "parameters": [
                {"name": "date", "in": "query", "schema": {"type": "string"}},
                {"name": "symbol", "in": "query", "schema": {"type": "string"}},
                {"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok"}}}},
            "/signal/panel": {"get": {"operationId": "querySignalPanel",
                "summary": "近 N 天新闻信号回测面板（命中率/超额）",
                "parameters": [
                {"name": "days", "in": "query", "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok"}}}},
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"FinDataTool/{VERSION}"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # CORS 预检
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, q = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/health":
                return self._send(200, {"status": "ok"})
            if path == "/openapi.json":
                return self._send(200, build_openapi(
                    self.headers.get("Host", "127.0.0.1:8001")))
            handlers = {
                "/stock/daily": handle_daily,
                "/stock/fundamental": handle_fundamental,
                "/stock/financial": handle_financial,
                "/kb/search": handle_kb_search,
                "/kb/documents": handle_kb_docs,
                "/market/snapshot": handle_market_snapshot,
                "/news/tickers": handle_news_tickers,
                "/signal/panel": handle_signal_panel,
            }
            fn = handlers.get(path)
            if not fn:
                return self._send(404, {"error": "not found",
                                        "paths": sorted(handlers)})
            return self._send(200, fn(q))
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[fin-tool] {self.address_string()} {fmt % args}\n")


def main(argv=None):
    p = argparse.ArgumentParser(prog="data_tool_server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8001)
    args = p.parse_args(argv)
    db.ensure_quant_schema(os.environ.get("KB_DB_PATH") or db.get_db_path())
    db.ensure_market_schema(os.environ.get("KB_DB_PATH") or db.get_db_path())
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"金融数据查询工具已启动: http://{args.host}:{args.port} "
          f"(health: /health, OpenAPI: /openapi.json)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())


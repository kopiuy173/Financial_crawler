#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""data_fetcher.py — 统一量化/基本面数据接入模块"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

_FILE = Path(__file__).resolve()
PY_DIR = _FILE.parent                       # .../dify_migration/python
MIG_DIR = PY_DIR.parent                     # .../dify_migration
PROJ_ROOT = MIG_DIR.parent                  # .../financial-crawler
CONFIG_PATH = os.environ.get(
    "DF_CONFIG", str(MIG_DIR / "config" / "data_source_config.yaml")
)

sys.path.insert(0, str(PROJ_ROOT))          # 使 `import database` 命中既有模块
os.chdir(PROJ_ROOT)                          # data/... 相对路径与既有系统一致

import pandas as pd                          # noqa: E402
import database as db                        # noqa: E402


# 配置
def load_config(path=CONFIG_PATH) -> dict:
    """读取 YAML 配置；文件缺失/损坏时给出可操作报错。"""
    import yaml  # 延迟导入，避免脚本被 `--help` 之外的场景拖慢

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {p}\n请先复制并修改 config/data_source_config.yaml"
        )
    with p.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if "sources" not in cfg or "watchlist" not in cfg:
        raise ValueError("配置缺少 sources / watchlist 段，请检查 YAML 结构")
    return cfg


def get_db_path() -> str:
    return os.environ.get("KB_DB_PATH") or db.get_db_path()


def get_conn(db_path: str | None = None):
    return db.get_connection(db_path or get_db_path())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 通用工具
def _retry(times=3, backoff_base=2.0, delay=1.0):
    """指数退避重试装饰器：失败间隔 delay*backoff_base**i 秒。"""

    def deco(fn):
        def wrapper(*args, **kwargs):
            last = None
            for i in range(max(1, int(times))):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001 — 单点失败要吞掉记录
                    last = e
                    if i < int(times) - 1:
                        wait = delay * (backoff_base ** i)
                        time.sleep(min(wait, 30))
            raise last
        return wrapper
    return deco


def _f(x, default=None):
    """安全转 float：None/NaN/空 → None。"""
    try:
        if x is None or (isinstance(x, float) and x != x):  # NaN
            return default
        f = float(x)
        return default if f != f else f          # 防御 pandas NA
    except (TypeError, ValueError):
        return default


def _s(x) -> str:
    """安全转字符串（去空白）。"""
    return "" if x is None else str(x).strip()


def split_cn_symbol(symbol: str) -> tuple[str, str]:
    """'600519.SH' → ('600519', 'SH')；容错无后缀形式。"""
    symbol = symbol.strip().upper()
    if "." in symbol:
        code, exch = symbol.split(".", 1)
        return code.strip(), exch.strip().upper()
    code = symbol
    if code.startswith(("6", "5", "9")) or code.startswith(("SH", "SH.")):
        return code, "SH"
    if code.startswith(("0", "2", "3")):
        return code, "SZ"
    return code, "SH"


def akshare_symbol(symbol: str) -> str:
    """akshare 的 A 股接口用纯 6 位代码（如 600519）。"""
    return split_cn_symbol(symbol)[0]



_DAILY_COLS = ["open", "high", "low", "close", "volume", "amount", "adj_close"]


def df_to_daily_rows(df: pd.DataFrame, symbol: str, source: str) -> list[dict]:
    """把含 trade_date 与 OHLCV 列的 DataFrame 规范化为落库行。"""
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        d = r.to_dict()
        out.append({
            "symbol": symbol,
            "source": source,
            "trade_date": _s(d.get("trade_date") or d.get("date") or d.get("日期")),
            "open": _f(d.get("open", d.get("Open"))),
            "high": _f(d.get("high", d.get("High"))),
            "low": _f(d.get("low", d.get("Low"))),
            "close": _f(d.get("close", d.get("Close"))),
            "volume": _f(d.get("volume", d.get("Volume"))),
            "amount": _f(d.get("amount", d.get("成交额"))),
            "adj_close": _f(d.get("adj_close", d.get("Adj Close"))),
            "extra": json.dumps({"source_cols": [c for c in d if c not in _DAILY_COLS]},
                                ensure_ascii=False),
        })
    return [row for row in out if row["trade_date"]]


def _fmt_date(d: str | None, dash=True) -> str:
    """容忍 20240101 / 2024-01-01 / Timestamp → YYYY-MM-DD。"""
    if not d:
        return ""
    s = str(d).strip()[:10].replace("/", "-").replace(".", "-")
    if "-" in s:
        return s
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _ak_hist_df(ak, symbol: str, start: str, end: str) -> pd.DataFrame:
    """akshare 历史行情 → 统一列 DataFrame。"""
    raw = ak.stock_zh_a_hist(
        symbol=akshare_symbol(symbol),
        period="daily",
        start_date=start.replace("-", ""),
        end_date=end.replace("-", ""),
        adjust="qfq",               # 前复权，便于走势与指标口径一致
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    return raw.rename(columns={
        "日期": "trade_date", "开盘": "open", "收盘": "close", "最高": "high",
        "最低": "low", "成交量": "volume", "成交额": "amount",
        "涨跌幅": "pct_chg", "换手率": "turnover",
    })


@_retry()
def fetch_akshare_daily(symbol: str, start: str, end: str) -> list[dict]:
    """AKShare 日线（免费，无需密钥）。"""
    import akshare as ak
    df = _ak_hist_df(ak, symbol, start, end)
    return df_to_daily_rows(df, symbol, "akshare")


@_retry()
def fetch_baostock_daily(symbol: str, start: str, end: str) -> list[dict]:
    """Baostock 日线（免费，无需密钥；登录一次，分批查询）。"""
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            baostock_symbol(symbol),
            "date,open,high,low,close,volume,amount,adjustflag",
            start_date=start, end_date=end, frequency="d", adjustflag="2",  # 前复权
        )
        if rs.error_code != "0":
            raise RuntimeError(f"baostock 查询失败: {rs.error_msg}")
        rows = []
        while rs.next():
            d = rs.get_row_data()
            rows.append({
                "trade_date": _fmt_date(d[0]), "open": _f(d[1]), "high": _f(d[2]),
                "low": _f(d[3]), "close": _f(d[4]), "volume": _f(d[5]),
                "amount": _f(d[6]), "adj_close": _f(d[4]),
            })
        return [{"symbol": symbol, "source": "baostock", **r} for r in rows]
    finally:
        bs.logout()


def fetch_tushare_daily(symbol: str, start: str, end: str) -> list[dict]:
    """Tushare Pro 日线（前复权；需 TUSHARE_TOKEN，并开通 daily/pro_bar 权限）。"""
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("未设置环境变量 TUSHARE_TOKEN，请先 export TUSHARE_TOKEN=\"API KEY\"（替换为真实密钥）")
    import tushare as ts
    ts.set_token(token)
    pro = ts.pro_api()
    df = ts.pro_bar(
        ts_code=symbol, api=pro, adj="qfq",
        start_date=start.replace("-", ""), end_date=end.replace("-", ""),
    )
    if df is None or df.empty:
        return []
    df = df.rename(columns={"vol": "volume"})
    df["trade_date"] = df["trade_date"].map(_fmt_date)
    df["adj_close"] = df["close"]
    return df_to_daily_rows(df, symbol, "tushare")


def _alpha_url(function: str, symbol: str, extra: dict | None = None) -> str:
    key = os.environ.get("ALPHA_VANTAGE_KEY")
    if not key:
        raise RuntimeError("未设置环境变量 ALPHA_VANTAGE_KEY")
    params = {"function": function, "symbol": symbol, "apikey": key,
              **(extra or {})}
    import requests
    resp = requests.get("https://www.alphavantage.co/query", params=params,
                        timeout=30)
    resp.raise_for_status()
    return resp.json()


@_retry()
def fetch_alpha_daily(symbol: str, start: str, end: str) -> list[dict]:
    """Alpha Vantage 日线（免费层 25 次/天；美股/外汇/指数等）。"""
    data = _alpha_url("TIME_SERIES_DAILY_ADJUSTED", symbol,
                      {"outputsize": "full"})
    series = data.get("Time Series (Daily)") or {}
    if not series:
        raise RuntimeError(f"Alpha Vantage 返回空/异常: {str(data)[:200]}")
    rows = []
    for date in sorted(series):
        if date < start or date > end:
            continue
        v = series[date]
        rows.append({
            "symbol": symbol, "source": "alpha_vantage",
            "trade_date": date,
            "open": _f(v.get("1. open")), "high": _f(v.get("2. high")),
            "low": _f(v.get("3. low")), "close": _f(v.get("4. close")),
            "adj_close": _f(v.get("5. adjusted close")),
            "volume": _f(v.get("6. volume")),
            "amount": None,
            "extra": json.dumps({"dividend": _f(v.get("7. dividend amount"))},
                                ensure_ascii=False),
        })
    return rows


@_retry()
def fetch_yfinance_daily(symbol: str, start: str, end: str) -> list[dict]:
    """yfinance 日线（免费；美股/港股/全球指数）。"""
    import yfinance as yf
    df = yf.download(
        symbol, start=start, end=end, interval="1d", auto_adjust=False,
        progress=False, group_by="ticker",
    )
    if df is None or df.empty:
        return []
    df = df.reset_index()
    # 单标的时列名直接是 Open/High/...；多标的兼容
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]  # 取第一个 ticker
    df = df.rename(columns={
        "Date": "trade_date", "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume", "Adj Close": "adj_close",
    })
    df["trade_date"] = df["trade_date"].map(_fmt_date)
    df["amount"] = None
    return df_to_daily_rows(df, symbol, "yfinance")


# 基本面快照（估值/市值）与定期财务指标
def _pick(cols, *fragments):
    """在列名里按关键词找第一个命中列，找不到返回 None。"""
    for c in cols:
        if any(f in c for f in fragments):
            return c
    return None


def _num_series(df, col):
    """把列安全转 numeric（无效→NaN），返回 Series。"""
    if not col or col not in df.columns:
        return pd.Series([None] * len(df), index=df.index)
    return pd.to_numeric(df[col], errors="coerce")


def fetch_akshare_fundamental(symbol: str) -> list[dict]:
    """AKShare 每日估值指标 stock_a_indicator_lg（A 股，免费）。"""
    import akshare as ak
    code = akshare_symbol(symbol)
    df = ak.stock_a_indicator_lg(symbol=code)
    if df is None or df.empty:
        return []
    df["trade_date"] = df["trade_date"].map(_fmt_date)
    rows = []
    for _, r in df.iterrows():
        d = r.to_dict()
        rows.append({
            "symbol": symbol, "source": "akshare",
            "trade_date": _fmt_date(d.get("trade_date")),
            "pe_ttm": _f(d.get("pe_ttm")), "pe": _f(d.get("pe")),
            "pb": _f(d.get("pb")), "ps_ttm": _f(d.get("ps_ttm")),
            "market_cap": None if _f(d.get("total_mv")) is None else _f(d.get("total_mv")) * 1e4,
            "float_market_cap": None if _f(d.get("circ_mv")) is None else _f(d.get("circ_mv")) * 1e4,
            "dividend_yield": _f(d.get("dv_ratio")),
            "extra": json.dumps({"unit": "mv=万元→元换算", "dv_ratio": "%"}, ensure_ascii=False),
        })
    return [x for x in rows if x["trade_date"]]


@_retry()
def fetch_akshare_financial(symbol: str) -> list[dict]:
    """AKShare 财务分析指标 stock_financial_analysis_indicator。"""
    import akshare as ak
    df = ak.stock_financial_analysis_indicator(symbol=akshare_symbol(symbol))
    if df is None or df.empty:
        return []
    cols = list(df.columns)
    date_col = _pick(cols, "日期", "报告期")
    if not date_col:
        raise RuntimeError(f"未识别到报告期列: {cols[:8]}")
    out = []
    for _, r in df.head(12).iterrows():
        d = r.to_dict()
        rev = _pick(cols, "营业总收入", "主营业务收入")
        npf = _pick(cols, "净利润", "归属于母公司")
        out.append({
            "symbol": symbol, "source": "akshare",
            "report_date": _fmt_date(d.get(date_col)),
            "revenue": _f(d.get(rev)) if rev else None,
            "net_profit": _f(d.get(npf)) if npf else None,
            "roe": _f(d.get(_pick(cols, "净资产收益率"))),
            "roa": None,
            "gross_margin": _f(d.get(_pick(cols, "毛利率"))),
            "net_margin": _f(d.get(_pick(cols, "净利率"))),
            "eps": _f(d.get(_pick(cols, "每股收益"))),
            "bps": _f(d.get(_pick(cols, "每股净资产"))),
            "debt_ratio": _f(d.get(_pick(cols, "资产负债率"))),
            "operating_cashflow": None,
            "extra": json.dumps({"note": "比率类按源口径(%)，报告期取自源字段"},
                                ensure_ascii=False),
        })
    return [x for x in out if x["report_date"]]


@_retry()
def fetch_tushare_fundamental(symbol: str, start: str, end: str) -> list[dict]:
    """Tushare daily_basic：每日 PE/PB/市值快照。"""
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("未设置环境变量 TUSHARE_TOKEN")
    import tushare as ts
    ts.set_token(token)
    pro = ts.pro_api()
    df = pro.daily_basic(
        ts_code=symbol, start_date=start.replace("-", ""),
        end_date=end.replace("-", ""),
        fields="ts_code,trade_date,close,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,"
               "total_share,float_share,total_mv,circ_mv,turnover_rate",
    )
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        d = r.to_dict()
        rows.append({
            "symbol": symbol, "source": "tushare",
            "trade_date": _fmt_date(d.get("trade_date")),
            "pe_ttm": _f(d.get("pe_ttm")), "pe": _f(d.get("pe")),
            "pb": _f(d.get("pb")), "ps_ttm": _f(d.get("ps_ttm")),
            "market_cap": None if _f(d.get("total_mv")) is None else _f(d.get("total_mv")) * 1e4,
            "float_market_cap": None if _f(d.get("circ_mv")) is None else _f(d.get("circ_mv")) * 1e4,
            "dividend_yield": _f(d.get("dv_ratio")),
            "total_share": None if _f(d.get("total_share")) is None else _f(d.get("total_share")) * 1e4,
            "float_share": None if _f(d.get("float_share")) is None else _f(d.get("float_share")) * 1e4,
            "extra": json.dumps({"close": _f(d.get("close"))}, ensure_ascii=False),
        })
    return [x for x in rows if x["trade_date"]]


@_retry()
def fetch_tushare_financial(symbol: str, periods: int = 8) -> list[dict]:
    """Tushare fina_indicator + income：比率与利润表关键项（积分需够）。"""
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("未设置环境变量 TUSHARE_TOKEN")
    import tushare as ts
    ts.set_token(token)
    pro = ts.pro_api()
    ind = pro.fina_indicator(
        ts_code=symbol,
        fields="ts_code,end_date,eps,bps,roe,roe_wa,roa,grossprofit_margin,"
               "netprofit_margin,debt_to_assets",
    )
    if ind is None or ind.empty:
        return []
    ind = ind.sort_values("end_date", ascending=False).head(int(periods))
    inc = pro.income(
        ts_code=symbol,
        fields="ts_code,end_date,revenue,n_income_attr_p,operate_cash_flow",
    )
    inc_map = {}
    if inc is not None and not inc.empty:
        inc_map = {
            r["end_date"]: r
            for _, r in inc.sort_values("end_date", ascending=False).head(int(periods)).iterrows()
        }
    rows = []
    for _, r in ind.iterrows():
        d = r.to_dict()
        inc_r = inc_map.get(d.get("end_date"), {})
        rows.append({
            "symbol": symbol, "source": "tushare",
            "report_date": _fmt_date(d.get("end_date")),
            "revenue": _f(inc_r.get("revenue")),
            "net_profit": _f(inc_r.get("n_income_attr_p")),
            "roe": _f(d.get("roe_wa") or d.get("roe")),
            "roa": _f(d.get("roa")),
            "gross_margin": _f(d.get("grossprofit_margin")),
            "net_margin": _f(d.get("netprofit_margin")),
            "eps": _f(d.get("eps")), "bps": _f(d.get("bps")),
            "debt_ratio": _f(d.get("debt_to_assets")),
            "operating_cashflow": _f(inc_r.get("operate_cash_flow")),
            "extra": json.dumps({"unit": "revenue/net_profit/ocf=元"}, ensure_ascii=False),
        })
    return rows


@_retry()
def fetch_alpha_fundamental(symbol: str) -> list[dict]:
    """Alpha Vantage OVERVIEW：美股公司概况（PE/市值/EPS 等，近似 TTM）。"""
    data = _alpha_url("OVERVIEW", symbol)
    if not data or not data.get("Symbol"):
        raise RuntimeError(f"Alpha Vantage OVERVIEW 异常: {str(data)[:200]}")
    today = datetime.now().strftime("%Y-%m-%d")
    dy_raw = _s(data.get("DividendYield")).replace("%", "")
    dy = _f(dy_raw) if dy_raw else None
    return [{
        "symbol": symbol.upper(), "source": "alpha_vantage",
        "trade_date": today,
        "pe_ttm": _f(data.get("PERatio")), "pe": _f(data.get("PERatio")),
        "pb": _f(data.get("PriceToBookRatio")),
        "ps_ttm": _f(data.get("PriceToSalesRatioTTM")),
        "market_cap": _f(data.get("MarketCapitalization")),
        "dividend_yield": dy,
        "extra": json.dumps({
            "eps": _f(data.get("EPS")),
            "revenue_ttm": _f(data.get("RevenueTTM")),
            "name": data.get("Name"),
        }, ensure_ascii=False),
    }]


# 源能力表与调度
SOURCE_META = {
    "akshare":        {"market": "cn",     "daily": True,  "fund": True, "fin": True},
    "tushare":        {"market": "cn",     "daily": True,  "fund": True, "fin": True},
    "baostock":       {"market": "cn",     "daily": True,  "fund": False, "fin": False},
    "alpha_vantage":  {"market": "global", "daily": True,  "fund": True, "fin": False},
    "yfinance":       {"market": "global", "daily": True,  "fund": False, "fin": False},
}
CN_GROUPS = ("cn_stocks", "cn_index")
GLOBAL_GROUPS = ("hk_us_stocks", "global_index")
WL_GROUPS = CN_GROUPS + GLOBAL_GROUPS

_FETCHERS = {
    "akshare": {"daily": fetch_akshare_daily, "fund": fetch_akshare_fundamental,
                "fin": fetch_akshare_financial},
    "tushare": {"daily": fetch_tushare_daily, "fund": fetch_tushare_fundamental,
                "fin": fetch_tushare_financial},
    "baostock": {"daily": fetch_baostock_daily},
    "alpha_vantage": {"daily": fetch_alpha_daily, "fund": fetch_alpha_fundamental},
    "yfinance": {"daily": fetch_yfinance_daily},
}


def enabled_sources(cfg, market: str | None = None) -> list[str]:
    names = [n for n, c in cfg["sources"].items() if c.get("enabled", False)]
    if market:
        names = [n for n in names if SOURCE_META[n]["market"] == market]
    return names


def pick_symbols(cfg, group: str | None = None, symbols=None):
    wl = cfg["watchlist"]
    if symbols:
        return [s.strip() for s in symbols]
    if group:
        return list(wl.get(group, []))
    return [s for g in wl for s in wl[g]]


def _default_range(cfg, days: int | None, symbol: str, conn) -> tuple[str, str]:
    """增量日期范围：有库内数据则从上一次之后拉取，否则回看 default_start。"""
    end = datetime.now().strftime("%Y-%m-%d")
    if days:
        start = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    else:
        row = conn.execute(
            "SELECT MAX(trade_date) AS m FROM stock_daily WHERE symbol=?", (symbol,)
        ).fetchone()
        if row and row["m"]:
            start = (datetime.strptime(row["m"], "%Y-%m-%d")
                     - timedelta(days=3)).strftime("%Y-%m-%d")   # 留 3 天容错补漏
        else:
            start = cfg.get("global", {}).get("default_start", "2015-01-01")
    return start, end


def _run_one(conn, cfg, source, symbol, task, start=None, end=None,
             dry_run=False) -> tuple[str, int]:
    """执行单源单标的单任务；返回 (status, rows)。异常一律记录且不中断。"""
    fn = _FETCHERS.get(source, {}).get(task)
    if not fn:
        return "skipped", 0
    try:
        kwargs = {}
        if task == "daily":
            kwargs.update(start=start, end=end)
        elif task == "fund" and source == "tushare":
            kwargs.update(start=start, end=end)
        rows = fn(symbol, **kwargs) if kwargs else fn(symbol)
        rows = rows or []
        if dry_run:
            return "ok", len(rows)
        with conn:
            if task == "daily":
                n = db.upsert_daily(conn, rows)
            elif task == "fund":
                n = db.upsert_fundamental(conn, rows)
            else:
                n = db.upsert_financial(conn, rows)
        db.log_sync(conn, source, task, symbol, "ok", len(rows), f"upsert={n}")
        return "ok", len(rows)
    except Exception as e:  # noqa: BLE001 — 单点失败不中断整体
        try:
            db.log_sync(conn, source, task, symbol, "error", 0, f"{e}")
        except Exception:  # noqa: BLE001
            pass
        return "error", 0


def refresh(cfg, tasks=("daily",), days=None, group=None, symbols=None,
            dry_run=False, retries=3):
    """批量刷新。返回汇总报告 dict。"""
    cfg_retry = cfg.get("global", {}).get("retry", {})
    retries = int(cfg_retry.get("times", retries) or retries)
    conn = get_conn()
    report = {"tasks": [], "ok": 0, "error": 0, "skipped": 0, "rows": 0}
    symbols = pick_symbols(cfg, group, symbols)
    for task in tasks:
        market = None
        if group in CN_GROUPS:
            market = "cn"
        elif group in GLOBAL_GROUPS:
            market = "global"
        for source in enabled_sources(cfg, market):
            if not SOURCE_META[source].get(task):
                continue
            for symbol in symbols:
                start, end = (None, None)
                if task == "daily":
                    start, end = _default_range(cfg, days, symbol, conn)
                st, n = _run_one(conn, cfg, source, symbol, task,
                                 start=start, end=end, dry_run=dry_run)
                report[st] += 1
                report["rows"] += n
                tag = f"[{source}] {symbol} {task}: {st} rows={n}"
                print(("(dry)" if dry_run else "") + tag)
                report["tasks"].append(tag)
    conn.close()
    return report


# CLI
def _ensure_libs_installed(cfg):
    """检查各启用源对应第三方库是否可用，返回缺失清单。"""
    missing = []
    for name in enabled_sources(cfg):
        try:
            if name == "akshare":
                __import__("akshare")
            elif name == "tushare":
                __import__("tushare")
            elif name == "baostock":
                __import__("baostock")
            elif name == "yfinance":
                __import__("yfinance")
            elif name == "alpha_vantage":
                pass            # 纯 requests 实现
        except ImportError:
            missing.append(name)
    return missing


def cmd_status(cfg, args):
    conn = get_conn()
    print("== 数据源状态 ==")
    for name, meta in SOURCE_META.items():
        c = cfg["sources"].get(name, {})
        on = c.get("enabled", False)
        print(f"  {name:<14} enabled={on} market={meta['market']}")
    miss = _ensure_libs_installed(cfg)
    if miss:
        print("缺失依赖库:", ", ".join(miss))
        print("  pip install " + " ".join(miss))
    print("\n== 库表行数 ==")
    for t in ("documents", "document_chunks", "stock_daily",
              "stock_fundamental", "stock_financial", "data_sync_log"):
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:  # noqa: BLE001
            n = "无表"
        print(f"  {t:<20} {n}")
    print(f"\nDB: {get_db_path()}")
    conn.close()


def cmd_stats(cfg, args):
    conn = get_conn()
    print("== stock_daily 覆盖（最近日期）==")
    for r in conn.execute(
        "SELECT symbol, source, COUNT(*) cnt, MAX(trade_date) latest "
        "FROM stock_daily GROUP BY symbol, source ORDER BY symbol").fetchall():
        print(f"  {r['symbol']:<12} {r['source']:<14} {r['cnt']:>6} 行 至 {r['latest']}")
    print("\n== stock_fundamental / stock_financial ==")
    for t in ("stock_fundamental", "stock_financial"):
        for r in conn.execute(
                f"SELECT symbol, COUNT(*) cnt FROM {t} GROUP BY symbol ORDER BY symbol").fetchall():
            print(f"  [{t}] {r['symbol']:<12} {r['cnt']} 行")
    conn.close()


def cmd_single(cfg, args, task):
    """daily / fundamental / financial 单命令。"""
    sym_opt = list(getattr(args, "symbols_opt", None) or [])
    symbols = sym_opt or args.symbols or pick_symbols(cfg, args.group)
    conn = get_conn()
    ok = err = rows_n = 0
    for symbol in symbols:
        source = args.source
        if source is None:  # 按市场自动选第一个可用源
            s_up = symbol.upper()
            market = "cn" if (s_up.endswith((".SH", ".SZ", ".BJ")) or s_up[0].isdigit()) \
                else "global"
            cands = enabled_sources(cfg, market)
            if task == "daily":
                cands = [s for s in cands if SOURCE_META[s].get("daily")]
            source = cands[0] if cands else None
        if not source:
            print(f"跳过 {symbol}: 无可用源")
            continue
        if not SOURCE_META.get(source, {}).get(task):
            print(f"跳过 {symbol}: {source} 不支持 {task}")
            continue
        start, end = (None, None)
        if task == "daily":
            start, end = _default_range(cfg, args.days, symbol, conn)
        st, n = _run_one(conn, cfg, source, symbol, task,
                         start=start, end=end, dry_run=args.dry_run)
        print(f"[{source}] {symbol} {task}: {st} rows={n}")
        ok += st == "ok"
        err += st == "error"
        rows_n += n
    conn.close()
    print(f"完成: ok={ok} err={err} 入库/试算行数={rows_n}")
    return 1 if err else 0


def build_parser():
    p = argparse.ArgumentParser(prog="data_fetcher",
                                description="金融数据统一接入（AKShare/Tushare/Alpha Vantage/Baostock/yfinance）")
    p.add_argument("--config", default=CONFIG_PATH, help="YAML 配置路径")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="检查源/库/依赖")
    sp.set_defaults(fn=lambda c, a: cmd_status(c, a) or 0)

    sp = sub.add_parser("stats", help="数据覆盖统计")
    sp.set_defaults(fn=lambda c, a: cmd_stats(c, a) or 0)

    for name, task in (("daily", "daily"), ("fundamental", "fund"), ("financial", "fin")):
        sp = sub.add_parser(name, help=f"拉取并落库 {name} 数据")
        sp.add_argument("symbols", nargs="*", help="标的，如 600519.SH AAPL；缺省用 watchlist")
        sp.add_argument("--symbol", action="append", dest="symbols_opt", default=None,
                        metavar="SYMBOL", help="指定标的（可多次，与位置参数等价）")
        sp.add_argument("--source", choices=list(SOURCE_META), default=None)
        sp.add_argument("--group", choices=WL_GROUPS, default=None)
        sp.add_argument("--days", type=int, default=None, help="回看天数（缺省=增量）")
        sp.add_argument("--dry-run", action="store_true", help="只拉取不入库")
        sp.set_defaults(fn=lambda c, a, t=task: cmd_single(c, a, t))

    sp = sub.add_parser("refresh", help="按 watchlist 批量刷新")
    sp.add_argument("--tasks", default="daily", help="逗号分隔: daily,fundamental,financial")
    sp.add_argument("--days", type=int, default=None)
    sp.add_argument("--group", choices=WL_GROUPS, default=None)
    sp.add_argument("--symbols", nargs="*", default=None)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=_cmd_refresh)
    return p


def _cmd_refresh(cfg, args):
    tasks = tuple(t.strip() for t in args.tasks.split(",") if t.strip())
    report = refresh(cfg, tasks=tasks, days=args.days, group=args.group,
                     symbols=args.symbols, dry_run=args.dry_run)
    print(f"\n汇总: ok={report['ok']} error={report['error']} "
          f"skipped={report['skipped']} 行数={report['rows']}")
    return 1 if report["error"] else 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        # 每次进入先确保量化表存在（幂等）
        db.ensure_quant_schema(get_db_path())
        return int(args.fn(cfg, args) or 0)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"运行失败: {e}", file=sys.stderr)
        if os.environ.get("DF_DEBUG"):
            traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())






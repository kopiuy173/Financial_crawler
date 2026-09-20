#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""需求2 综合系统跑通测试（临时库隔离，离线确定性为主）。"""
from __future__ import annotations
import json, os, sys, tempfile, time, urllib.request, subprocess
from datetime import datetime
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.chdir(PROJ)
sys.path.insert(0, str(PROJ))

import database as db
import market_snapshot as ms
import ticker_link as tl
import signal_outcomes as so

def _test_log(msg):
    print(f"[{ms.now_local():%F %T}] {msg}", flush=True)
ms.log = _test_log

KEEP = os.environ.get("REQ2_TEST_KEEP", "0") == "1"
TMP_DB = os.environ.get("KB_DB_PATH") or str(
    Path(PROJ) / "data" / "tmp" / f"req2_e2e_{int(time.time())}.db")
os.environ["KB_DB_PATH"] = TMP_DB
db.init_db(TMP_DB)
REPORT_DIR = str(Path(TMP_DB).with_suffix(""))
cfg = ms.load_config()
cfg["reports"] = {"dir": REPORT_DIR, "days": 90}
FAIL = []

def ok(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAIL.append(name)
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))

def sec(title):
    print(f"\n===== {title} =====")

sec("0) 临时库与状态初始化")
conn = db.get_connection(TMP_DB)
cal = ms.load_trade_days()
today = ms.now_local(cfg).strftime("%Y-%m-%d")
hist = [d for d in cal if d <= today]
n = len(hist)
ok("交易日历可用", n > 30, f"hist 共 {n} 天，今日={today} 最近={hist[-1]}")
sd_n1, sd_n2 = hist[-4], hist[-2]
print(f"  确定性日期：sd_n1={sd_n1} sd_n2={sd_n2}（相对最近交易日锚定）")

univ = [
    {"symbol": "600519.SH", "name": "贵州茅台", "exchange": "SH"},
    {"symbol": "000002.SZ", "name": "万科A", "exchange": "SZ"},
    {"symbol": "300750.SZ", "name": "宁德时代", "exchange": "SZ"},
    {"symbol": "600036.SH", "name": "招商银行", "exchange": "SH"},
    {"symbol": "002594.SZ", "name": "比亚迪", "exchange": "SZ"},
    {"symbol": "000001.SZ", "name": "平安银行", "exchange": "SZ"},
]
db.upsert_ticker_universe(conn, univ)
ok("A股字典写入", conn.execute("SELECT COUNT(*) c FROM ticker_universe").fetchone()["c"] == 6)

docs = [
    {"id": "tst_news_1", "source_type": "news", "title": "贵州茅台公告重大利好：拟增持回购并提价，订单大幅增长",
     "content": "贵州茅台今日宣布回购计划并上调出厂价10%，多家机构称超预期。",
     "publish_date": sd_n1, "metadata": {"date": "10:30", "source": "test"}},
    {"id": "tst_news_2", "source_type": "news", "title": "某公司发布例行经营数据说明",
     "content": "公司管理层表示当前运营正常，无重大事项。",
     "publish_date": sd_n2, "metadata": {"date": "16:00", "source": "test"}},
    {"id": "tst_news_3", "source_type": "news", "title": "万科Ａ午后股价波动，公司回应市场关切",
     "content": "万科Ａ股价今日异动，公告澄清传闻。",
     "publish_date": sd_n1, "metadata": {"date": "13:05", "source": "test"}},
]
for d in docs:
    db.insert_document(conn, dict(d))
ok("文档写入", db.count_by_source_type(conn).get("news") == 3)

win = hist[-6:]
s_rows, b_rows = [], []
for k, dt in enumerate(win):
    s_rows.append({"symbol": "600519.SH", "source": "akshare", "trade_date": dt,
                   "open": 1000.0 + k, "high": 1000.0 + k, "low": 1000.0 + k,
                   "close": 1000.0 + k, "volume": 10000 + k, "amount": 1e8})
    b_rows.append({"symbol": "000300.SH", "source": "akshare_index", "trade_date": dt,
                   "open": 3000.0 + 5 * k, "high": 3000.0 + 5 * k, "low": 3000.0 + 5 * k,
                   "close": 3000.0 + 5 * k, "volume": 2e6, "amount": 2e11})
db.upsert_daily(conn, s_rows)
db.upsert_daily(conn, b_rows)
ok("个股/基准日线预置", db.query_daily(conn, "600519.SH", source="akshare")[-1]["close"] == 1000.0 + len(win) - 1)

today_snap = [
    {"trade_date": hist[-1], "slot": "15:00", "kind": "index", "code": "000001.SH",
     "name": "上证指数", "close": 3100.12, "pct_chg": 0.35, "volume": 4.1e11, "amount": 4.1e11},
    {"trade_date": hist[-1], "slot": "15:00", "kind": "industry_board", "code": "BK0475",
     "name": "酿酒行业", "close": 1200.0, "pct_chg": 1.2, "volume": 1e10, "amount": 1e10},
]
db.upsert_snapshot_rows(conn, today_snap)
print(f"  快照今日 {hist[-1]} 行数：{db.snapshot_done(conn, hist[-1], '15:00')}")
print(f"  临时库：{TMP_DB}")
conn.close()

sec("1) 新闻→标的 自动关联（ticker_link）")
conn = db.get_connection(TMP_DB)
link1 = tl.link_date(conn, cfg, publish_date=sd_n1)      # tst_news_1 + tst_news_3
link2 = tl.link_date(conn, cfg, publish_date=sd_n2)      # 无匹配词 → 0
links = db.news_ticker_rows(conn, publish_date=sd_n1)
ok("link_date 返回统计", link1.get("links") == 2, f"links={link1.get('links')}")
ok("次日窗口幂等跳过", link2.get("links") == 0, f"links={link2.get('links')}")
link1b = tl.link_date(conn, cfg, publish_date=sd_n1)
ok("重复关联不叠加", link1b.get("links") == 0 and len(db.news_ticker_rows(conn, publish_date=sd_n1)) == 2)
print("  关联明细：")
for r in links:
    print(f"    {r['news_id']:<12} {r['ticker_name']:<6} {r['symbol']:<12} matched_in={r['matched_in']}")
ok("万科Ａ 全角字匹配", any(r["news_id"] == "tst_news_3" and r["symbol"] == "000002.SZ" for r in links))
conn.close()

sec("2) 信号批量展开（build，horizons 1/3/5/10）")
conn = db.get_connection(TMP_DB)
b1 = so.build_signals(conn, cfg, publish_date=sd_n1)
b2 = so.build_signals(conn, cfg, publish_date=sd_n2)
ok("sd_n1 建信号 8 条", b1 == 8, f"built={b1}")  # 2条新闻 × 4 持有期
ok("sd_n2（无关联）0 条", b2 == 0, f"built={b2}")
status_rows = conn.execute("SELECT status, COUNT(*) c FROM signal_outcomes GROUP BY status").fetchall()
print(f"  展开后状态：" + ", ".join(f"{r['status']}={r['c']}" for r in status_rows))
row_up = conn.execute("SELECT direction FROM signal_outcomes WHERE news_id='tst_news_1' LIMIT 1").fetchone()
ok("词表方向=up", row_up and row_up["direction"] == "up")
conn.close()

sec("3) 结算 compute（纯日历预判 → 只拉到期标的 → 精确/error 分路）")
conn = db.get_connection(TMP_DB)
net_calls = []
_orig_ens, _orig_ensb = so.ensure_symbol_daily, so.ensure_bench_daily
def _fake_ensure_symbol(conn, sym, start, end):
    net_calls.append(("symbol", sym))
    return 0
def _fake_ensure_bench(conn, cfg, start, end):
    net_calls.append(("bench", (cfg.get("benchmark") or {}).get("symbol")))
    return 0
so.ensure_symbol_daily = _fake_ensure_symbol
so.ensure_bench_daily = _fake_ensure_bench
t0 = time.perf_counter()
res = so.compute_pending(conn, cfg)
el = (time.perf_counter() - t0) * 1000
print(f"  compute 耗时 {el:.1f} ms，结果={res}")
print(f"  行情保障调用：{net_calls}（万科A缺行情 → error，未到期行 → 不拉）")
ok("只拉今日可结算标的+基准", sorted(x[1] for x in net_calls if x[0] == "symbol") == ["000002.SZ", "600519.SH"]
   and any(x[0] == "bench" for x in net_calls),
   f"calls={[c[1] for c in net_calls]}")
ok("h1/h3 结算 + error 分路", res["filled"] == 2 and res["error"] == 2,
   f"filled={res['filled']} error={res['error']}")
f = conn.execute("SELECT horizon_days, entry_date, exit_date, symbol_pct, bench_pct, excess_pct "
                 "FROM signal_outcomes WHERE news_id='tst_news_1' AND status='filled' ORDER BY horizon_days").fetchall()
for r in f:
    print(f"    h{r['horizon_days']:<2} {r['entry_date']}→{r['exit_date']} 个股 {r['symbol_pct']:+.4f}% "
          f"基准 {r['bench_pct']:+.4f}% 超额 {r['excess_pct']:+.4f}%")
ok("h1 entry/exit 锚点正确", f[0]["entry_date"] == hist[-4] and f[0]["exit_date"] == hist[-3])
exp_h1 = exp_h3 = 0.0
for k, dt in enumerate(win):
    if dt == hist[-4]:
        exp_h1 = ((1000.0 + k + 1) / (1000.0 + k) - 1.0) * 100.0
        exp_h3 = ((1000.0 + k + 3) / (1000.0 + k) - 1.0) * 100.0
ok("h1 个股涨跌幅精确", abs(f[0]["symbol_pct"] - round(exp_h1, 4)) < 1e-9,
   f"got={f[0]['symbol_pct']:.4f} exp={exp_h1:.4f}")
ok("h3 个股涨跌幅精确", abs(f[1]["symbol_pct"] - round(exp_h3, 4)) < 1e-9,
   f"got={f[1]['symbol_pct']:.4f} exp={exp_h3:.4f}")
err = conn.execute("SELECT status, note FROM signal_outcomes WHERE news_id='tst_news_3' AND horizon_days=1").fetchone()
ok("万科A缺行情 → error 定格", err["status"] == "error" and "缺行情" in err["note"], f"note={err['note']}")
res2 = so.compute_pending(conn, cfg)
ok("重复 compute 幂等（0 新增，不拉行情）", res2["filled"] == 0 and res2["remain"] == 4,
   f"filled={res2['filled']} remain={res2['remain']}")
net_calls.clear()
so.ensure_symbol_daily, so.ensure_bench_daily = _orig_ens, _orig_ensb
conn.close()

sec("4) 回测面板 report（临时目录输出）")
conn = db.get_connection(TMP_DB)
payload = so.build_report(conn, cfg)
conn.close()
paths = sorted(Path(REPORT_DIR).glob("signal_eval_*.*")) if Path(REPORT_DIR).exists() else []
print(f"  面板生成 {len(paths)} 个文件：" + ", ".join(p.name for p in paths[:4]))
for row in payload["rows"]:
    print(f"    dir={row['direction']:<8} h{row['horizon_days']:<2} n={row['n']} "
          f"hit={row['hit_rate']} avg={row['avg_symbol_pct']:+.2f}% excess={row['avg_excess_pct']:+.2f}%")
ok("面板含 filled 2 条", payload["total_filled"] == 2)

sec("5) 性能对照（批量 vs 逐行事务）")
conn = db.get_connection(TMP_DB)
cands = [{"news_id": f"perf_{i}", "symbol": "600036.SH", "horizon_days": 1,
          "direction": "neutral", "direction_method": "lexicon",
          "signal_date": hist[-1], "signal_time": "", "bench_symbol": "000300.SH"}
         for i in range(2000)]
t_b = time.perf_counter()
db.add_signals_batch(conn, cands)
el_b = (time.perf_counter() - t_b) * 1000
t_s = time.perf_counter()
for c in cands[:200]:
    db.upsert_signal(conn, c)
el_s = (time.perf_counter() - t_s) * 1000
print(f"  add_signals_batch 2000 条：{el_b:.1f} ms")
print(f"  逐行 upsert_signal 200 条：{el_s:.1f} ms（单条平均 {(el_s/200)*1000:.0f} us）")
print(f"  = 批量较逐行快约 {el_s / max(el_b / 10, 1e-9):.0f} 倍（同量级折算）")
conn.execute("DELETE FROM signal_outcomes WHERE news_id LIKE 'perf_%'")
conn.commit()

sec("6) market_snapshot.tick 空转开销（守护 30s 轮询主路径）")
_idle_date = ms.now_local(cfg).strftime("%Y-%m-%d")
state_done = {"date": _idle_date, "slots": {
    s: {"date": _idle_date, "status": "done"} for s in
    [p["time"] for p in cfg["snapshot_points"]]}}
t0 = time.perf_counter()
for _ in range(500):
    out = ms.tick(cfg=cfg, conn=None, state=dict(state_done))
el_t = (time.perf_counter() - t0) * 1000
ok("tick 空转不产生动作", out == [])
print(f"  500 次空转调用共 {el_t:.1f} ms（单次 {el_t/500:.2f} ms，无 DB 连接/无状态写盘）")

sec("7) 守护 evening 收尾一键（build+compute+report 幂等冒烟）")
try:
    import news_scheduler as sch  # noqa: F401
    ok("调度器模块可导入 + _market_cfg()", isinstance(sch._market_cfg(), dict))
except Exception as e:  # noqa: BLE001
    ok("调度器模块可导入", False, f"{type(e).__name__}: {e}")

sec("8) CLI 状态接口（交互端冒烟）")
env = dict(os.environ)
env["KB_DB_PATH"] = TMP_DB
for label, cmd in [
    ("market_snapshot --status", [sys.executable, "market_snapshot.py", "--status"]),
    ("signal_outcomes --status", [sys.executable, "signal_outcomes.py", "--status"]),
]:
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    print(f"  --- {label} rc={r.returncode} ---")
    print((r.stdout or r.stderr).strip()[:400])
    ok(f"CLI {label}", r.returncode == 0)

sec("9) HTTP ToolServer 端点（数据上传/Agent 交互端联通）")
PORT = 18787
proc = subprocess.Popen(
    [sys.executable, "dify_migration/python/data_tool_server.py", "--host", "127.0.0.1",
     "--port", str(PORT)], env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
base = f"http://127.0.0.1:{PORT}"
up = False
for _ in range(40):
    try:
        urllib.request.urlopen(base + "/health", timeout=1)
        up = True
        break
    except Exception:
        time.sleep(0.25)
ok("server 启动", up)
from urllib.parse import quote
for path in ["/market/snapshot?trade_date=" + hist[-1] + "&slot=15:00",
             "/news/tickers?date=" + sd_n1,
             "/signal/panel?days=90",
             "/kb/search?q=" + quote("茅台"),
             "/openapi.json"]:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cnt = data.get("count") or data.get("total_filled") or (
            len(data.get("data", [])) if isinstance(data.get("data"), list) else "-")
        print(f"  GET {path}  -> {resp.status}  count={cnt}")
        ok("endpoint " + path.split("?")[0], resp.status == 200)
    except Exception as e:
        ok("endpoint " + path.split("?")[0], False, str(e))
proc.terminate()
try:
    proc.wait(timeout=10)
except Exception:
    proc.kill()

print("\n" + "=" * 30)
if FAIL:
    print(f"FAILED {len(FAIL)} 项：{FAIL}")
    sys.exit(1)
print("ALL PASS")
if not KEEP:
    try:
        Path(TMP_DB).unlink()
        for p in (Path(REPORT_DIR).glob("signal_eval_*.*") if Path(REPORT_DIR).exists() else []):
            p.unlink()
        if Path(REPORT_DIR).exists() and not any(Path(REPORT_DIR).iterdir()):
            Path(REPORT_DIR).rmdir()
    except Exception:  # noqa: BLE001
        pass
sys.exit(0)

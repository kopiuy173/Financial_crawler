#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""news_scheduler.py — 每日三时段新闻抓取调度器（常驻守护 / 单次补跑）"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

PROJ = Path(__file__).resolve().parent
os.chdir(PROJ)

import database  # noqa: E402  （storage_resolve / 默认库路径，D 盘优先）
import crawler  # noqa: E402  （load_schedule / schedule_window / window_cutoff_dt / main）
import market_snapshot  # noqa: E402  （需求2：指数/板块快照 tick）
import ticker_link  # noqa: E402  （需求2：新闻→标的关联）
import signal_outcomes  # noqa: E402 （需求2：信号结算 + 回测面板）
import predict  # noqa: E402 （需求3：盘前主题预测 → 收盘复盘 → LLM 反思）
import model_interface as mi  # noqa: E402 （复用 _secret：环境变量 → ~/.deepseek_key → 项目 .env）

LOGS_DIR = PROJ / "logs"
STATE_PATH = LOGS_DIR / "pipeline_state.json"
LOCK_PATH = LOGS_DIR / "pipeline.lock"
NEWS_LOG = LOGS_DIR / "daily_news.log"
SYNC_SCRIPT = PROJ / "dify_migration" / "python" / "sync_news_daily.py"
PYTHON = sys.executable

DIFY_API_KEY = mi._secret("DIFY_API_KEY")
KB_DB_PATH = os.environ.get("KB_DB_PATH") or database.get_db_path()


# 时间与状态
def local_now():
    """Asia/Shanghai 当地时间（naive）。ZoneInfo 不可用时回退机器本地时间。"""
    tz_name = crawler.load_schedule().get("timezone") or "Asia/Shanghai"
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.now()


def today_str():
    return local_now().strftime("%Y-%m-%d")


def log(msg):
    """写一条带时间戳的记录：同时进 logs/daily_news.log 与当前 stdout。"""
    line = f"[{local_now():%F %T}] {msg}"
    LOGS_DIR.mkdir(exist_ok=True)
    with open(NEWS_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log(" 状态文件损坏，按空状态重建")
    return {
        "date": "",
        "day_quota_bytes": int(crawler.load_schedule()["daily_quota_bytes"]),
        "day_used_bytes": 0,
        "windows": {},
    }


def save_state(state):
    LOGS_DIR.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(STATE_PATH)


def effective_left(state):
    """当日剩余额度；跨天后重置。额度以 config/schedule.yaml 实时值为准。"""
    quota = int(crawler.load_schedule().get("daily_quota_bytes") or 0)
    if state.get("date") != today_str():
        state["date"] = today_str()
        state["day_used_bytes"] = 0
    return max(0, quota - int(state.get("day_used_bytes") or 0))


def _at_dt(win, base=None):
    base = base or local_now()
    hh, mm = [int(x) for x in str(win.get("at") or "09:00").split(":")[:2]]
    return base.replace(hour=hh, minute=mm, second=0, microsecond=0)


def due_windows(state, sched):
    """按 schedule.yaml 顺序返回今天已到期且尚未完成的窗口配置列表。"""
    now = local_now()
    date = today_str()
    due = []
    for win in sched["windows"]:
        wid = win.get("id", "")
        done = (state.get("windows", {}).get(wid, {}) or {}).get("date") == date
        if not done and now >= _at_dt(win, now):
            due.append(win)
    return due


def window_status(state):
    ids = [w["id"] for w in crawler.load_schedule()["windows"]]
    return {wid: (state.get("windows", {}).get(wid) or {}) for wid in ids}


def run_due_windows(state, sched, dry_run=False):
    due = due_windows(state, sched)
    if not due:
        log(" 当前没有到期的窗口")
        return []

    sites = crawler.get_enabled_sites(crawler.load_config())
    n_sources = max(1, len(sites))
    results = []
    date = today_str()

    for idx, win in enumerate(due):
        wid = win["id"]
        remaining = len(due) - idx
        left = effective_left(state)
        slot = int(left / (remaining * n_sources))
        log(f" 窗口 {wid} 到期：剩余额度 {left} B ÷ {remaining} 窗口 ÷ "
            f"{n_sources} 来源 = 每源配额 {slot} B")
        if dry_run:
            results.append({"window": wid, "slot": slot, "saved": 0, "bytes": 0})
            continue

        LOGS_DIR.mkdir(exist_ok=True)
        with open(NEWS_LOG, "a", encoding="utf-8") as f:
            with contextlib.redirect_stdout(f):
                try:
                    report = crawler.main(vectorize=True, window_id=wid,
                                          source_budget_bytes=slot)
                except Exception as e:
                    report = {"window": wid, "saved": 0, "bytes": 0}
                    f.write(f" 窗口 {wid} 抓取异常: {e}\n")

        used = int(report.get("bytes") or 0)
        quota_now = int(sched.get("daily_quota_bytes") or 0)

        _run_window_market_tasks(date, wid)

        state.setdefault("windows", {})[wid] = {
            "date": date,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "saved": int(report.get("saved") or 0),
            "bytes": used,
        }
        state["day_used_bytes"] = min(
            int(state.get("day_used_bytes") or 0) + used,
            max(quota_now, 0))
        save_state(state)
        log(f" 窗口 {wid} 完成：新增 {report.get('saved', 0)} 条 / "
            f"{used} B（当日累计 {state['day_used_bytes']} B）")
        results.append({"window": wid, "slot": slot,
                        "saved": report.get("saved", 0), "bytes": used})

    return results


def _market_cfg():
    """读取 market.yaml；失败或未启用返回空配置。"""
    try:
        cfg = market_snapshot.load_config()
    except Exception as e:  # noqa: BLE001
        log(f" 读取 market.yaml 失败: {e}")
        return {"enabled": False}
    return cfg if isinstance(cfg, dict) else {"enabled": False}


def _predict_cfg():
    """读取 predict.yaml；失败或非 dict 返回空配置（各功能自动禁用）。"""
    try:
        cfg = predict.load_config()
    except Exception as e:  # noqa: BLE001
        log(f" 读取 predict.yaml 失败: {e}")
        return {"enabled": False}
    return cfg if isinstance(cfg, dict) else {"enabled": False}


def _run_window_market_tasks(date, wid):
    """每个真实新闻窗口收尾（幂等，可安全重跑）：
    - 任意窗口：为当日新入库新闻做 标的关联（若 enable）
    - evening：  15:00 收盘快照宽限补录 → 信号结算/回测面板
                → watchlist 日线刷新 → Dify 当日同步（既有逻辑）
    """
    cfg = _market_cfg()
    if not cfg.get("enabled"):
        if wid == "evening":
            run_dify_sync(date)   # market 关闭不影响既有 Dify 同步
        return
    hooks = cfg.get("hooks") or {}

    if hooks.get("auto_link", True):
        try:
            conn = market_snapshot.get_conn()
            try:
                ticker_link.link_date(conn, cfg, publish_date=date)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log(f" 标的关联异常: {e}")

    if wid == "morning" and hooks.get("auto_predict_morning", True):
        try:
            for a in predict.tick(_predict_cfg()):
                log(f" 盘前预测收尾: {a.get('action')}{a.get('detail') or ''}")
        except Exception as e:  # noqa: BLE001
            log(f" 盘前预测异常: {e}")

    if wid != "evening":
        return

    try:
        market_snapshot.tick(cfg=cfg)   # 15:00 收盘快照宽限补录（幂等）
    except Exception as e:  # noqa: BLE001
        log(f" 收盘快照补录异常: {e}")

    try:
        import board_members as bm
        bres = bm.ensure_fresh(cfg=(cfg.get("board_members") or {}))
        if bres.get("action") == "refresh":
            log(f" 板块成分: 成功 {bres.get('ok')} 跳过 {bres.get('skipped')} "
                f"写入 {bres.get('members')} 行")
    except Exception as e:  # noqa: BLE001
        log(f" 板块成分异常: {e}")

    try:
        import board_members as bm
        pres = bm.ensure_predicted(date, cfg=(cfg.get("board_members") or {}))
        if pres.get("action") == "refresh":
            log(f" 预测主题成分: 板块 {len(pres.get('predicted') or [])} 个，"
                f"写入 {pres.get('members')} 行")
    except Exception as e:  # noqa: BLE001
        log(f" 预测主题成分异常: {e}")

    if hooks.get("auto_signals", True) or hooks.get("auto_compute", True):
        if hooks.get("refresh_watchlist", True):
            _refresh_watchlist()
        try:
            res = signal_outcomes.evening_stage(cfg, publish_date=date)
            log(f" 信号/回测收尾: built={res.get('built')} "
                f"filled={res.get('compute', {}).get('filled')} "
                f"report_total={res.get('report_total_filled')}")
        except Exception as e:  # noqa: BLE001
            log(f" 信号/回测异常: {e}")
    elif hooks.get("refresh_watchlist", True):
        _refresh_watchlist()

    if hooks.get("auto_predict_evening", True):
        try:
            res = predict.evening_stage(_predict_cfg(), date=date)
            st = (res.get("settle") or {})
            log(f" 预测/复盘收尾: evaluated={st.get('evaluated', 0)} "
                f"hit_l={st.get('hit', {}).get('leading')} "
                f"hit_r={st.get('hit', {}).get('risk')} "
                f"reflect_rows={len(res.get('reflect', {}).get('rows', []) or [])}")
        except Exception as e:  # noqa: BLE001
            log(f" 预测/复盘异常: {e}")

    if hooks.get("auto_baseline", True):
        _run_baseline(date)

    run_dify_sync(date)   # 既有：当日新闻同步 Dify kb-金融新闻


def _refresh_watchlist():
    """best-effort 刷新 watchlist 日线（复用 data_fetcher refresh）。"""
    import subprocess
    dfx = PROJ / "dify_migration" / "python" / "data_fetcher.py"
    if not dfx.exists():
        log(" 未找到 data_fetcher.py，跳过 watchlist 刷新")
        return
    env = dict(os.environ)
    env.setdefault("KB_DB_PATH", KB_DB_PATH)
    cmd = [PYTHON, str(dfx), "refresh", "--tasks", "daily", "--days", "1"]
    try:
        with open(NEWS_LOG, "a", encoding="utf-8") as f:
            rc = subprocess.call(cmd, env=env, stdout=f, stderr=f, timeout=1200)
        log(f" watchlist 日线刷新结束 rc={rc}")
    except Exception as e:  # noqa: BLE001
        log(f" watchlist 日线刷新失败: {e}")


def _market_tick_poll():
    """守护轮询里的行情快照检查（幂等）；返回本次动作。"""
    cfg = _market_cfg()
    if not cfg.get("enabled"):
        return []
    try:
        acts = market_snapshot.tick(cfg=cfg)
        for a in acts:
            detail = f"（{a.get('rows')} 行）" if "rows" in a else ""
            log(f" 行情快照轮询: {a.get('slot')} → {a.get('action')}{detail}")
        try:
            import stock_snapshot as ss
            sres = ss.run(cfg=(cfg.get("stock_snapshot") or {}))
            if sres.get("action") in ("snapshot", "missed"):
                log(f" 个股快照: {sres.get('action')} 行数={sres.get('rows')}")
        except Exception as e:  # noqa: BLE001
            log(f" 个股快照异常: {e}")
        return acts
    except Exception as e:  # noqa: BLE001
        log(f" 行情快照轮询异常: {e}")
        return []


def _predict_morning_poll():
    """守护轮询里的盘前预测检查（幂等，08:40→09:10 目标、09:30 兜底）。"""
    cfg = _predict_cfg()
    if not cfg.get("enabled"):
        return []
    try:
        acts = predict.tick(cfg)
        for a in acts:
            log(f" 盘前预测轮询: {a.get('action')}{a.get('detail') or ''}")
        return acts
    except Exception as e:  # noqa: BLE001
        log(f" 盘前预测轮询异常: {e}")
        return []


def _run_baseline(date):
    """评估层 P1：evening 收尾后自动跑基线对照（只读，失败不影响主链路）。"""
    try:
        import predict_baseline as pb
        res = pb.run_for_date(date)
        log(" 基线对照: " + json.dumps(res, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        log(f" 基线对照异常: {e}")


def run_dify_sync(date):
    log(f" 开始同步当日新闻到 Dify kb-金融新闻（{date}）")
    if not SYNC_SCRIPT.exists():
        log(f" 未找到 {SYNC_SCRIPT}，跳过同步")
        return
    if not DIFY_API_KEY:
        log(" 未配置 DIFY_API_KEY（环境变量 / ~/.deepseek_key / 项目 .env），跳过同步")
        return
    import subprocess
    env = dict(os.environ)
    env.update({"KB_DB_PATH": KB_DB_PATH, "DIFY_API_KEY": DIFY_API_KEY})
    cmd = [PYTHON, str(SYNC_SCRIPT), "--date", date]
    with open(NEWS_LOG, "a", encoding="utf-8") as f:
        rc = subprocess.call(cmd, env=env, stdout=f, stderr=f)
    log(f" Dify 同步结束 rc={rc}")


# 入口
def _acquire_lock():
    import fcntl
    LOGS_DIR.mkdir(exist_ok=True)
    fh = open(LOCK_PATH, "w", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(" 已有另一个调度实例在运行（logs/pipeline.lock），退出")
        sys.exit(0)
    return fh


def main():
    p = argparse.ArgumentParser(description="金融快讯三时段调度器")
    p.add_argument("--daemon", action="store_true",
                   help="常驻守护：循环检查到期窗口（默认）")
    p.add_argument("--once", action="store_true", help="执行全部到期窗口后退出")
    p.add_argument("--dry-run", action="store_true", help="只打印计划不抓取")
    p.add_argument("--status", action="store_true", help="打印状态并退出")
    args = p.parse_args()

    if args.status:
        st = load_state()
        print(json.dumps({
            "today": today_str(),
            "day_used_bytes": st.get("day_used_bytes", 0),
            "day_quota_bytes": int(crawler.load_schedule()["daily_quota_bytes"]),
            "windows": window_status(st),
            "due_now": [w["id"] for w in
                        due_windows(st, crawler.load_schedule())],
        }, ensure_ascii=False, indent=2))
        return

    sched = crawler.load_schedule()
    poll = int(sched.get("window_poll_seconds") or 30)
    state = load_state()
    fh = _acquire_lock()

    if args.dry_run:
        run_due_windows(state, sched, dry_run=True)
        print(json.dumps({"day_used_bytes": state.get("day_used_bytes", 0)},
                         ensure_ascii=False))
        return

    log(f" 调度器启动（date={today_str()}，轮询 {poll}s）")
    try:
        while True:
            ran = run_due_windows(state, sched)
            macts = _market_tick_poll()   # 需求2：到点/宽限内抓行情快照
            pacts = _predict_morning_poll()  # 需求3：08:40 盘前生成当日主题预测
            if args.once:
                if not ran and not macts and not pacts:
                    break
            time.sleep(poll)
    except KeyboardInterrupt:
        log(" 调度器收到中断，退出")
    finally:
        fh.close()


if __name__ == "__main__":
    main()

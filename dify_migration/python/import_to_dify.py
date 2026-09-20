#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""import_to_dify.py — 把现有 SQLite 知识库迁移到 Dify 数据集"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_FILE = Path(__file__).resolve()
PY_DIR = _FILE.parent
MIG_DIR = PY_DIR.parent
PROJ_ROOT = MIG_DIR.parent
sys.path.insert(0, str(PROJ_ROOT))
os.chdir(PROJ_ROOT)

import requests                                # noqa: E402
import database as db                          # noqa: E402

TYPE_LABELS = {
    "news": "金融新闻",
    "paper": "研究论文",
    "report": "券商研报",
    "textbook": "金融教材",
}

DIFY_BASE = os.environ.get("DIFY_BASE", "http://127.0.0.1")
API_BASE = f"{DIFY_BASE.rstrip('/')}/v1"


# Dify HTTP 助手
def _call(method, path, token, json_body=None, timeout=120, retries=4):
    """带重试的 Dify API 调用；HTTP 错误抛 RuntimeError（含响应体摘要）。"""
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    last = None
    for i in range(retries):
        try:
            resp = requests.request(method, url, headers=headers,
                                    json=json_body, timeout=timeout)
        except requests.RequestException as e:
            last = RuntimeError(f"网络错误 {url}: {e}")
            time.sleep(2 * (i + 1))
            continue
        if resp.status_code in (200, 201):
            return resp.json() if resp.text else {}
        if resp.status_code in (401, 403):
            raise RuntimeError(f"认证失败({resp.status_code}): 请检查 DIFY_API_KEY / DIFY_BASE\n{resp.text[:300]}")
        if resp.status_code in (429, 500, 502, 503):
            last = RuntimeError(f"临时错误({resp.status_code}): {resp.text[:200]}")
            time.sleep(3 * (i + 1))
            continue
        raise RuntimeError(f"Dify API {method} {url} 失败({resp.status_code}): {resp.text[:300]}")
    raise last


def list_datasets(token, limit=100):
    datasets, page = [], 1
    while True:
        data = _call("GET", f"/datasets?page={page}&limit={limit}", token)
        batch = data.get("data", [])
        datasets.extend(batch)
        if len(batch) < limit or page >= data.get("total_pages", page):
            break
        page += 1
    return datasets


def ensure_dataset(token, name, indexing):
    """按名称查找或创建数据集，返回其 id。"""
    for d in list_datasets(token):
        if d.get("name") == name:
            print(f"  数据集已存在: {name} ({d.get('id')})")
            return d["id"]
    body = {
        "name": name,
        "indexing_technique": indexing,   # high_quality / economy
        "permission": "only_me",
    }
    data = _call("POST", "/datasets", token, json_body=body)
    ds_id = data.get("id")
    print(f"  新建数据集: {name} ({ds_id})")
    return ds_id


def upload_document_part(token, ds_id, name, text, indexing="high_quality"):
    """把一个 part 作为单个 Dify 文档上传。

    part 内原 chunk 以空行（\\n\\n）连接，故走 custom 分段规则按 \\n\\n 切回，
    让 Dify 索引粒度与源库 chunk 一致，避免 automatic 二次切碎/重叠。
    """
    body = {
        "name": name,
        "text": text,
        "indexing_technique": indexing,
        "process_rule": {
            "mode": "custom",
            "rules": {
                "pre_processing_rules": [],
                "segmentation": {"separator": "\n\n", "max_tokens": 2000},
            },
        },
    }
    data = _call("POST", f"/datasets/{ds_id}/document/create_by_text", token,
                 json_body=body, timeout=300)
    return data.get("document") or data



# 读取 SQLite → 生成 part
def collect_parts(db_path, source_type, chunks_per_part=40, max_chars=300000,
                  inject_meta=False):
    """读取某 source_type 的全部文档，返回 [(name, text, doc_meta), ...]。

    长文档按连续 chunk 打包成多个 part（每 part ≤ chunks_per_part 段、
    ≤ max_chars 字符），part 名带序号，便于 Dify 单文档体积与检索粒度可控。
    """
    conn = db.get_connection(db_path)
    parts = []
    try:
        docs = conn.execute(
            "SELECT * FROM documents WHERE source_type=? ORDER BY created_at DESC",
            (source_type,),
        ).fetchall()
        if not docs:
            return parts
        for doc in docs:
            d = dict(doc)
            chunk_rows = conn.execute(
                "SELECT content FROM document_chunks WHERE document_id=? "
                "ORDER BY chunk_index ASC", (d["id"],),
            ).fetchall()
            if not chunk_rows:
                chunks = [d.get("content") or d.get("abstract") or ""]
            else:
                chunks = [r["content"] for r in chunk_rows]
            n_parts = max(1, -(-len(chunks) // max(1, int(chunks_per_part))))
            meta = {
                "doc_id": d["id"], "source_type": source_type,
                "title": d["title"], "url": d.get("url", ""),
                "publish_date": d.get("publish_date", ""),
                "tags": d.get("tags", ""), "created_at": d.get("created_at", ""),
                "n_chunks": len(chunks),
            }
            title = (d["title"] or d["id"])[:80]
            date = (d.get("publish_date") or "")[:10]
            for i in range(n_parts):
                seg = chunks[i * chunks_per_part:(i + 1) * chunks_per_part]
                text = "\n\n".join(c for c in seg if c and c.strip())
                if not text.strip():
                    continue
                if inject_meta:
                    head = (f"META | 类型={TYPE_LABELS.get(source_type, source_type)} "
                            f"| 日期={date} | 来源={d.get('url', '')} "
                            f"| 标签={d.get('tags', '')}\n\n")
                    text = head + text
                if len(text) > max_chars:      # 兜底硬截断，避免超 API 体积限制
                    text = text[:max_chars]
                part_label = f"p{i + 1}/{n_parts}" if n_parts > 1 else ""
                name = " · ".join(x for x in (title, date, part_label) if x)
                parts.append((name, text, dict(meta)))
    finally:
        conn.close()
    return parts


def load_manifest(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return set(json.load(f).get("done", []))
            except (ValueError, TypeError):
                return set()
    return set()


def save_manifest(path, done, append=None):
    done = set(done)
    if append:
        done.update(append)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done)}, f, ensure_ascii=False, indent=1)
    return done


# 主流程
def run(args):
    token = os.environ.get("DIFY_API_KEY")
    if not token and not args.dry_run:
        raise SystemExit("缺少 DIFY_API_KEY：请在 Dify 数据集页生成后 export DIFY_API_KEY=\"API KEY\"（替换为真实密钥）")
    global API_BASE
    if args.base:
        API_BASE = f"{args.base.rstrip('/')}/v1"

    manifest_path = Path(args.manifest)
    done = load_manifest(manifest_path) if not args.dry_run else set()
    include_types = (set(t.strip() for t in args.types.split(",") if t.strip())
                     if args.types else set(TYPE_LABELS))

    # 0) 汇总源库现状
    conn = db.get_connection(args.db)
    print("== 源库现状 ==")
    for st in TYPE_LABELS:
        n_doc = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE source_type=?", (st,)).fetchone()[0]
        n_chunk = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE document_id IN "
            "(SELECT id FROM documents WHERE source_type=?)", (st,)).fetchone()[0]
        print(f"  {TYPE_LABELS[st]:<6} 文档={n_doc:<4} 分块={n_chunk}")
    conn.close()

    total_upload = 0
    for st, label in TYPE_LABELS.items():
        if st not in include_types:
            continue
        parts = collect_parts(args.db, st, chunks_per_part=args.chunks_per_part,
                              inject_meta=args.inject_meta)
        if not parts:
            print(f"[{label}] 无数据，跳过")
            continue
        print(f"\n== [{label}] 共 {len(parts)} 个 part（{st}）==")
        if args.dry_run:
            for name, text, _m in parts[:3]:
                print(f"  [示例] {name}  ({len(text)} 字符)")
            total_upload += len(parts)
            continue

        ds_name = f"{args.prefix}-{label}"
        ds_id = ensure_dataset(token, ds_name, args.indexing)
        done_this = 0
        for name, text, meta in parts:
            key = f"{st}/{meta['doc_id']}/{name}"
            if key in done:
                print(f"  跳过(已完成): {name}")
                continue
            try:
                upload_document_part(token, ds_id, name, text, args.indexing)
            except Exception as e:  # noqa: BLE001
                print(f"  上传失败 {name}: {e}")
                continue
            done = save_manifest(manifest_path, done, append=[key])
            done_this += 1
            print(f"  已上传[{done_this}] {name} ({len(text)} 字符)")
            if args.wait:
                _wait_indexed(token, ds_id, args.wait)
        total_upload += done_this
        print(f"[{label}] 本次上传 {done_this} 个 part")

    print(f"\n完成。共处理 {total_upload} 个 part（dry-run 为计划数）。"
          f"\n请到 Dify 后台「知识库」查看各数据集索引状态。")
    return 0


def _wait_indexed(token, ds_id, timeout=120):
    """轮询该数据集最近文档的索引状态（异常不影响主流程返回）。"""
    deadline = time.time() + int(timeout)
    while time.time() < deadline:
        data = _call("GET", f"/datasets/{ds_id}/documents?page=1&limit=1", token)
        batch = data.get("data", [])
        if not batch:
            time.sleep(3)
            continue
        doc = batch[0]
        st = doc.get("indexing_status") or doc.get("status")
        if st in ("completed", "available"):
            return True
        if st in ("error", "failed"):
            print(f"  注意：最近文档索引异常: {doc.get('error') or st}")
            return False
        time.sleep(3)
    print("  等待索引超时（可稍后到后台查看）")
    return False


def build_parser():
    p = argparse.ArgumentParser(prog="import_to_dify",
                                description="SQLite 知识库 → Dify 数据集迁移")
    p.add_argument("--db", default=os.environ.get("KB_DB_PATH") or db.get_db_path(),
                   help="源 SQLite 路径")
    p.add_argument("--base", default=DIFY_BASE, help="Dify 入口，默认 http://127.0.0.1")
    p.add_argument("--prefix", default="kb", help="数据集名前缀")
    p.add_argument("--types", default="", help="逗号分隔: news,paper,report,textbook（默认全量）")
    p.add_argument("--indexing", default="high_quality",
                   choices=["high_quality", "economy"],
                   help="索引方式；未配 embedding 前可用 economy(关键词) 先跑通")
    p.add_argument("--chunks-per-part", type=int, default=40,
                   help="每个上传 part 包含的原始分块数")
    p.add_argument("--inject-meta", action="store_true",
                   help="把 类型/日期/URL/标签 作为 META 行注入 part 首行")
    p.add_argument("--wait", type=int, default=0,
                   help="每个 part 上传后轮询索引状态的秒数上限（0=不等）")
    p.add_argument("--manifest", default=str(MIG_DIR / "data" / "import_manifest.json"))
    p.add_argument("--dry-run", action="store_true", help="只统计计划，不调用 Dify")
    return p


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))




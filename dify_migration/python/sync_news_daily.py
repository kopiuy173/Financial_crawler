#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sync_news_daily.py — 把"某一天"新抓取的新闻（documents.source_type='news'）增量同步到 Dify 数据集 kb-金融新闻。"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

_FILE = Path(__file__).resolve()
PY_DIR = _FILE.parent
MIG_DIR = PY_DIR.parent
PROJ_ROOT = MIG_DIR.parent

sys.path.insert(0, str(PROJ_ROOT))
sys.path.insert(0, str(PY_DIR))

import database as db                                   # noqa: E402
import import_to_dify as imp                             # noqa: E402

DS_NAME = "kb-金融新闻"


def build_parser():
    p = argparse.ArgumentParser(prog="sync_news_daily",
                                description="某天抓取的新闻 → Dify kb-金融新闻 增量同步")
    p.add_argument("--date", default=date.today().isoformat(),
                   help="要同步的发布日期 YYYY-MM-DD（默认今天）")
    p.add_argument("--db", default=os.environ.get("KB_DB_PATH") or db.get_db_path(),
                   help="源 SQLite 路径")
    p.add_argument("--base", default=imp.DIFY_BASE, help="Dify 入口，默认 http://127.0.0.1")
    p.add_argument("--indexing", default="high_quality",
                   choices=["high_quality", "economy"])
    p.add_argument("--chunks-per-part", type=int, default=40)
    p.add_argument("--manifest",
                   default=str(MIG_DIR / "data" / "import_manifest.json"))
    p.add_argument("--dry-run", action="store_true")
    return p


def main():
    args = build_parser().parse_args()
    token = os.environ.get("DIFY_API_KEY")
    if not token and not args.dry_run:
        raise SystemExit("缺少 DIFY_API_KEY：请在 Dify 数据集页生成后 export DIFY_API_KEY=\"API KEY\"（替换为真实密钥）")
    if args.base:
        imp.API_BASE = f"{args.base.rstrip('/')}/v1"

    manifest_path = Path(args.manifest)
    done = imp.load_manifest(manifest_path) if not args.dry_run else set()

    parts = imp.collect_parts(args.db, "news",
                              chunks_per_part=args.chunks_per_part)
    target = [p for p in parts
              if (p[2].get("publish_date") or "")[:10] == args.date]

    print(f"== {args.date} 待同步新闻 ==")
    print(f"  源库中该日期新闻 part 数: {len(target)}")

    if args.dry_run:
        for name, text, m in target[:50]:
            print(f"  [计划] {name} ({len(text)} 字符, doc={m['doc_id']})")
        print(f"（dry-run 计划 {len(target)} 条）")
        return 0
    if not token:
        return 1

    ds_id = imp.ensure_dataset(token, DS_NAME, args.indexing)
    upload_count = 0
    for name, text, meta in target:
        key = f"news/{meta['doc_id']}/{name}"
        if key in done:
            print(f"  跳过(已完成): {name}")
            continue
        try:
            imp.upload_document_part(token, ds_id, name, text, args.indexing)
        except Exception as e:  # noqa: BLE001
            print(f"  上传失败 {name}: {e}")
            continue
        done = imp.save_manifest(manifest_path, done, append=[key])
        upload_count += 1
        print(f"  已上传[{upload_count}] {name} ({len(text)} 字符)")

    print(f"\n完成。本次新上传 {upload_count} 条 → {DS_NAME}（Dify 索引由 worker 异步进行）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

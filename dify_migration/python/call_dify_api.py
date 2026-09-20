#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""call_dify_api.py — 通过 Dify API 外部调用“金融分析助手”应用"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DIFY_BASE = os.environ.get("DIFY_BASE", "http://127.0.0.1").rstrip("/")
API_BASE = f"{DIFY_BASE}/v1"


def api_key() -> str:
    key = os.environ.get("DIFY_APP_API_KEY")
    if not key:
        raise SystemExit("缺少 DIFY_APP_API_KEY：请在 Dify 应用「API 访问」页生成并 export")
    return key


def headers() -> dict:
    return {"Authorization": f"Bearer {api_key()}"}


def upload_file(path: str, user: str) -> str:
    """上传本地文件到 Dify，返回 upload_file_id。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    import requests
    with p.open("rb") as f:
        resp = requests.post(
            f"{API_BASE}/files/upload",
            headers=headers(),
            files={"file": (p.name, f, "application/octet-stream")},
            data={"user": user},
            timeout=300,
        )
    resp.raise_for_status()
    fid = resp.json().get("id")
    print(f"[已上传] {p.name} → upload_file_id={fid}", file=sys.stderr)
    return fid


def guess_file_type(name: str) -> str:
    """按扩展名给 Dify files.type 提示（document/image/audio/video）。"""
    ext = Path(name).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return "image"
    if ext in (".mp3", ".wav", ".m4a"):
        return "audio"
    if ext in (".mp4", ".mov", ".webm"):
        return "video"
    return "document"


def chat(query: str, *, files=None, conversation_id="", user="financial-assistant",
         inputs=None, stream=False, timeout=600):
    import requests
    body = {
        "inputs": inputs or {},
        "query": query,
        "response_mode": "streaming" if stream else "blocking",
        "conversation_id": conversation_id or "",
        "user": user,
        "files": files or [],
    }
    resp = requests.post(
        f"{API_BASE}/chat-messages", headers=headers(),
        json=body, timeout=timeout, stream=stream,
    )
    resp.raise_for_status()
    if not stream:
        data = resp.json()
        conv_id = data.get("conversation_id", "")
        print(data.get("answer", ""))
        print(f"\n[conversation_id] {conv_id}", file=sys.stderr)
        return conv_id

    # SSE 流式：按行解析 data: {...}
    conv_id = ""
    buf = ""
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if evt.get("event") == "message":
            print(evt.get("answer", ""), end="", flush=True)
            buf += evt.get("answer", "")
            conv_id = evt.get("conversation_id", conv_id) or conv_id
        elif evt.get("event") == "message_end":
            conv_id = evt.get("conversation_id", conv_id) or conv_id
        elif evt.get("event") == "error":
            print(f"\n[流错误] {evt.get('message')}", file=sys.stderr)
            break
    print("\n[conversation_id] " + conv_id, file=sys.stderr)
    return conv_id


def build_parser():
    p = argparse.ArgumentParser(prog="call_dify_api", description="调用 Dify 金融分析助手")
    p.add_argument("query", help="提问内容")
    p.add_argument("--file", action="append", default=[], metavar="PATH",
                   help="本地文件（可多次），自动按类型以 local_file 上传")
    p.add_argument("--conversation", default="", help="会话 id（续聊）")
    p.add_argument("--style", default="", help="回答风格变量：精简/详细/创意/严谨")
    p.add_argument("--user", default="financial-assistant")
    p.add_argument("--blocking", action="store_true", help="非流式（默认流式）")
    p.add_argument("--base", default=DIFY_BASE, help="Dify 入口")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    global DIFY_BASE, API_BASE
    if args.base:
        DIFY_BASE = args.base.rstrip("/")
        API_BASE = f"{DIFY_BASE}/v1"

    files = []
    for fp in args.file:
        fid = upload_file(fp, args.user)
        files.append({"type": guess_file_type(fp), "transfer_method": "local_file",
                      "upload_file_id": fid})
    inputs = {"style": args.style} if args.style else {}
    stream = not args.blocking
    chat(args.query, files=files, conversation_id=args.conversation,
         user=args.user, inputs=inputs, stream=stream)
    return 0


if __name__ == "__main__":
    sys.exit(main())

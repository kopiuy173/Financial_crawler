"""query_knowledge.py — 统一检索接口"""

import os
import sqlite3
import json
import time

import database

INDEX_SERVICE_BASE = os.environ.get(
    "KB_INDEX_SERVICE", "http://127.0.0.1:8931").rstrip("/")
_SVC_PROBE_AT = 0.0   # time.monotonic() 上次探测时间
_SVC_UP = False

VALID_TYPES = ("news", "paper", "textbook", "report")

SNIPPET_LEN = 200


def _open_conn():
    return database.get_connection()


def _to_doc_dict(row):
    """sqlite3.Row → dict，并解析 metadata JSON。"""
    doc = dict(row)
    try:
        doc["metadata"] = json.loads(doc.get("metadata") or "{}")
    except (ValueError, TypeError):
        doc["metadata"] = {}
    return doc


def _snippet(text, keyword):
    """返回包含关键词的上下文片段。"""
    text = (text or "").strip()
    if not text:
        return ""
    low, kw = text.lower(), keyword.lower()
    idx = low.find(kw)
    if idx < 0:
        return text[:SNIPPET_LEN] + ("…" if len(text) > SNIPPET_LEN else "")
    start = max(0, idx - 60)
    end = min(len(text), idx + SNIPPET_LEN - 60)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


# 1. 关键词搜索
def search_by_keyword(keyword, source_type=None):
    """
    关键词全文搜索，可按来源类型过滤。

    返回: [{id, source_type, title, authors, abstract, content(截断),
            url, publish_date, tags, snippet, score}, ...]
    """
    if not keyword or not keyword.strip():
        return []
    keyword = keyword.strip()

    where = "WHERE "
    args = []
    cond = []
    if source_type:
        if source_type not in VALID_TYPES:
            raise ValueError(f"source_type 必须是 {VALID_TYPES} 之一")
        cond.append("source_type = ?")
        args.append(source_type)
    cond.append(
        "(title LIKE ? OR abstract LIKE ? OR content LIKE ? OR tags LIKE ?)"
    )
    like = f"%{keyword}%"
    args += [like, like, like, like]
    where += " AND ".join(cond)

    sql = (f"SELECT id, source_type, title, authors, abstract, content, "
           f"url, publish_date, tags, metadata FROM documents {where} "
           f"ORDER BY publish_date DESC, created_at DESC")

    conn = _open_conn()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    results = []
    for row in rows:
        doc = _to_doc_dict(row)
        content = doc.get("content") or ""
        title = doc.get("title") or ""
        score = 100.0 if keyword.lower() in title.lower() else 0.0
        score += (doc.get("abstract") or "").lower().count(keyword.lower()) * 10
        score += content.lower().count(keyword.lower())
        results.append({
            "id": doc["id"],
            "source_type": doc["source_type"],
            "title": doc["title"],
            "authors": doc.get("authors", ""),
            "abstract": doc.get("abstract", ""),
            "content": content,
            "url": doc.get("url", ""),
            "publish_date": doc.get("publish_date", ""),
            "tags": doc.get("tags", ""),
            "snippet": _snippet(content or doc.get("abstract"), keyword),
            "score": score,
            "metadata": doc.get("metadata", {}),
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def search_chunk_keyword(keyword, top_k=10, source_type=None):
    """
    关键词分块搜索（向量未启用时的降级实现）。
    返回 [{document_id, chunk_index, content, title, source_type, ...}]
    """
    if not keyword or not keyword.strip():
        return []
    key = f"%{keyword.strip()}%"
    sql = ("SELECT c.document_id, c.chunk_index, c.content, "
           "d.title, d.source_type, d.url, d.publish_date "
           "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
           "WHERE c.content LIKE ?")
    args = [key]
    if source_type:
        if source_type not in VALID_TYPES:
            raise ValueError(f"source_type 必须是 {VALID_TYPES} 之一")
        sql += " AND d.source_type = ?"
        args.append(source_type)
    sql += " ORDER BY c.content LIMIT ?"
    args.append(top_k * 3)
    conn = _open_conn()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows[:top_k]]


def _index_service_available():
    """探测常驻索引服务是否在线（在线缓存 30s，离线 5s 内不重复探测）。"""
    global _SVC_PROBE_AT, _SVC_UP
    now = time.monotonic()
    window = 30.0 if _SVC_UP else 5.0
    if now - _SVC_PROBE_AT < window:
        return _SVC_UP
    _SVC_PROBE_AT = now
    try:
        import requests
        resp = requests.get(INDEX_SERVICE_BASE + "/health", timeout=0.8)
        _SVC_UP = (resp.status_code == 200
                   and resp.json().get("status") == "ok")
    except Exception:
        _SVC_UP = False
    return _SVC_UP


def _remote_vector_search(query, top_k=10, source_type=None):
    """常驻索引服务检索；服务离线/异常返回 None（调用方回退进程内直连）。"""
    if not _index_service_available():
        return None
    try:
        import requests
        payload = {"query": query, "top_k": top_k}
        if source_type:
            payload["source_type"] = source_type
        resp = requests.post(INDEX_SERVICE_BASE + "/search",
                             json=payload, timeout=10)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


# 2. 向量搜索
def search_by_vector(query, top_k=10, source_type=None):
    """
    向量语义搜索。优先常驻索引服务（index_service.py），离线时回退进程内
    直接连接 Chroma；两者都不可用时自动降级为分块关键词搜索。

    返回: [{id, document_id, title, source_type, content, chunk_index,
            score, url, snippet}, ...]
    """
    if not query or not query.strip():
        return []

    res = _remote_vector_search(query, top_k=top_k, source_type=source_type)
    collection = None
    if res is None:
        try:
            from data_ingestion import _get_collection
            collection = _get_collection()
        except Exception:
            collection = None
        if collection is not None:
            kwargs = {
                "query_texts": [query.strip()],
                "n_results": top_k,
            }
            if source_type:
                if source_type not in VALID_TYPES:
                    raise ValueError(f"source_type 必须是 {VALID_TYPES} 之一")
                try:
                    kwargs["where"] = {"source_type": source_type}
                except Exception:
                    pass
            try:
                res = collection.query(**kwargs)
            except Exception as e:
                print(f" Chroma 查询失败，降级关键词：{e}")
                res = None
    has_hits = bool(
        res
        and res.get("ids")
        and any(rows or [] for rows in res["ids"])
    )
    if has_hits:
        out = []
        ids = res.get("ids")[0] if isinstance(res.get("ids"), list) and res["ids"] else []
        distances = (res.get("distances") or [[]])[0] if res.get("distances") else []
        documents = (res.get("documents") or [[]])[0] if res.get("documents") else []
        metadatas = (res.get("metadatas") or [[]])[0] if res.get("metadatas") else []
        doc_lookup = _fetch_docs([(m or {}).get("document_id", "") for m in metadatas] or [])
        for i, cid in enumerate(ids):
            md = metadatas[i] if i < len(metadatas) else {}
            did = md.get("document_id", "")
            doc = doc_lookup.get(did, {})
            dist = distances[i] if i < len(distances) else None
            chunk_text = documents[i] if i < len(documents) else ""
            out.append({
                "id": did,
                "document_id": did,
                "title": doc.get("title", md.get("title", "")),
                "source_type": doc.get("source_type", md.get("source_type", "")),
                "url": doc.get("url", ""),
                "content": doc.get("content", ""),
                "chunk_index": md.get("chunk_index", i),
                "snippet": _snippet(chunk_text, query),
                "distance": dist,
                "score": 1.0 / (1.0 + (dist if dist is not None else 0.0)),
                "metadata": {},
            })
        return out

    # Chroma 不可用：SQLite 分块关键词降级
    deg = search_chunk_keyword(query, top_k=top_k, source_type=source_type)
    out = []
    for i, r in enumerate(deg):
        out.append({
            "id": r["document_id"],
            "document_id": r["document_id"],
            "title": r.get("title", ""),
            "source_type": r.get("source_type", ""),
            "url": r.get("url", ""),
            "content": _doc_content(r["document_id"]),
            "chunk_index": r["chunk_index"],
            "snippet": _snippet(r.get("content", ""), query),
            "distance": None,
            "score": 1.0 / (i + 1.0),
            "metadata": {},
        })
    return out


def _fetch_docs(ids):
    if not ids:
        return {}
    conn = _open_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM documents WHERE id IN (%s)" % ",".join("?" * len(ids)),
            ids,
        ).fetchall()
    finally:
        conn.close()
    return {r["id"]: _to_doc_dict(r) for r in rows}


def _doc_content(doc_id):
    conn = _open_conn()
    try:
        row = conn.execute(
            "SELECT content FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    finally:
        conn.close()
    return row["content"] if row else ""


def _rrf_merge(lists_a, score_weights=(1.0, 1.0), k=60):
    """Reciprocal Rank Fusion 合并两路结果。"""
    scores = {}
    rank = {}
    for primary, weight in ((lists_a[0], score_weights[0]), (lists_a[1], score_weights[1])):
        for i, item in enumerate(primary):
            doc_id = item.get("document_id") or item.get("id")
            if not doc_id:
                continue
            if doc_id not in rank:
                rank[doc_id] = {}
            rank[doc_id][id(primary)] = i + 1
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + i + 1)
    merged = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return merged, rank


# 3. 混合搜索
def hybrid_search(query, top_k=10, keyword_weight=1.0, vector_weight=1.0):
    """
    混合搜索：关键词结果 + 向量结果，使用 RRF 融合。
    top 结果同时带关键词匹配片段与向量 snippet。
    """
    kw = search_by_keyword(query)  # 全量关键词命中
    vec = search_by_vector(query, top_k=max(top_k * 3, 20))  # 向量召回更多
    merged, rank = _rrf_merge((kw, vec), (keyword_weight, vector_weight))

    conn = _open_conn()
    try:
        keywords = [dict(r) for r in conn.execute(
            "SELECT id, source_type, title, authors, abstract, url, "
            "publish_date, tags, content FROM documents ORDER BY publish_date DESC"
        ).fetchall()]
    finally:
        conn.close()

    combined = {d["id"]: d for d in keywords}
    kw_by_id = {d.get("id"): d for d in kw}
    vec_by_id = {d.get("document_id") or d.get("id"): d for d in vec}

    results = []
    for doc_id, rr in merged:
        doc = combined.get(doc_id)
        if not doc:
            continue
        kw_item = kw_by_id.get(doc_id, {})
        vec_item = vec_by_id.get(doc_id, {})
        snippet = (vec_item.get("snippet") or kw_item.get("snippet")
                   or _snippet(doc.get("content", ""), query))
        results.append({
            "id": doc_id,
            "source_type": doc.get("source_type", ""),
            "title": doc.get("title", ""),
            "snippet": snippet,
            "url": doc.get("url", ""),
            "publish_date": doc.get("publish_date", ""),
            "rrf_score": rr,
            "keyword_hits": int(kw_item.get("score") or 0),
            "vector_distance": vec_item.get("distance"),
            "metadata": {},
        })
        if len(results) >= top_k:
            break
    return results


# 4. 统计
def get_statistics():
    """
    返回知识库统计：
      total_documents / by_source_type / total_chunks / total_raw_files / db_path
    """
    conn = _open_conn()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"]
        by_type_rows = conn.execute(
            "SELECT source_type, COUNT(*) AS c FROM documents GROUP BY source_type"
        ).fetchall()
        chunks = conn.execute(
            "SELECT COUNT(*) AS c FROM document_chunks"
        ).fetchone()["c"]
    finally:
        conn.close()

    by_source_type = {database.VALID_SOURCE_TYPES[i]: 0 for i in range(len(database.VALID_SOURCE_TYPES))}
    for r in by_type_rows:
        by_source_type[r["source_type"]] = r["c"]

    raw_counts = {}
    root = database.storage_resolve(
        "KB_RAW_ROOT", ("原始归档",), os.path.join("data", "raw"))
    if os.path.isdir(root):
        for typ in ("news", "papers", "textbooks", "reports"):
            d = os.path.join(root, typ)
            n = 0
            if os.path.isdir(d):
                for dirpath, _, files in os.walk(d):
                    n += len([f for f in files if not f.startswith(".")])
            raw_counts[typ] = n

    return {
        "total_documents": total,
        "by_source_type": by_source_type,
        "by_source_type_ordered": {k: by_source_type[k] for k in ("news", "paper", "textbook", "report")},
        "total_chunks": chunks,
        "raw_file_counts": raw_counts,
        "db_path": database.get_db_path(),
    }
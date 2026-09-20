"""data_ingestion.py — 通用数据接入接口"""

import os
import re
import json
import hashlib
import shutil
from datetime import datetime

import database

# 目录组织（支持环境变量覆盖，不再于 import 时创建）
RAW_ROOT = database.storage_resolve(
    "KB_RAW_ROOT", ("原始归档",), os.path.join("data", "raw"))
CHROMA_DIR = database.storage_resolve(
    "KB_CHROMA_DIR", ("检索库", "chroma"), os.path.join("data", "chroma"))
SOURCE_DIRS = {
    "news": os.path.join(RAW_ROOT, "news"),          # 子目录再按 YYYY-MM-DD
    "paper": os.path.join(RAW_ROOT, "papers"),
    "textbook": os.path.join(RAW_ROOT, "textbooks"),
    "report": os.path.join(RAW_ROOT, "reports"),
}


def _env_int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# 重建并自动刷新 SQLite 分块，保证两库对账一致。
# 分块 800/120（与存量一致；改动需重建向量索引）
CHUNK_SIZE = _env_int("KB_CHUNK_SIZE", 800)
CHUNK_OVERLAP = _env_int("KB_CHUNK_OVERLAP", 120)

KB_EMBEDDING_MODEL = os.environ.get(
    "KB_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)

# PDF 解析参数
PDF_PARSER = (os.environ.get("KB_PDF_PARSER", "auto") or "auto").strip().lower()
MIN_PAGE_CHARS = _env_int("KB_MIN_PAGE_CHARS", 20)


def ensure_raw_dirs():
    """确保 data/raw 各类型目录存在（可按需手动调用，或由各 ingest 自动创建）。"""
    os.makedirs(RAW_ROOT, exist_ok=True)
    for path in SOURCE_DIRS.values():
        os.makedirs(path, exist_ok=True)


# 通用工具
def _date_folder(source_type="news"):
    """
    返回目标存储目录：
      news → data/raw/news/YYYY-MM-DD/
      其他 → data/raw/{papers|textbooks|reports}/
    """
    base = SOURCE_DIRS[source_type]
    if source_type == "news":
        base = os.path.join(base, datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(base, exist_ok=True)
    return base


def _parse_news_date(json_data):
    """
    从新闻 JSON 中尽力解析发布日期（YYYY-MM-DD），失败返回空串。

    解析顺序：date → publish_date → firstPublishTime → crawled_at（最后的兜底），
    兼容 10/13 位时间戳、ISO 日期、中文“YYYY年M月D日”、纯 HH:MM 快讯时间。
    """
    raw_news = json_data if isinstance(json_data, dict) else {}

    for key in ("date", "publish_date", "firstPublishTime", "crawled_at"):
        val = raw_news.get(key)
        if not val:
            continue
        text = str(val).strip()
        if not text:
            continue
        if text.isdigit():
            try:
                ts = float(text)
                if ts > 1e12:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                continue
        # ISO / 常规 YYYY-MM-DD
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        # 中文 YYYY年M月D日
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if re.match(r"^\d{1,2}:\d{2}", text):
            return datetime.now().strftime("%Y-%m-%d")
    return ""


def _copy_to_raw(source_type, src_path, extra_name=None):
    """复制外部文件至 data/raw 对应类型目录，返回落盘路径。"""
    if not src_path or not os.path.exists(src_path):
        return src_path or ""
    target_dir = _date_folder(source_type)
    name = extra_name or os.path.basename(src_path)
    dest = os.path.join(target_dir, name)
    if os.path.abspath(dest) != os.path.abspath(src_path):
        shutil.copy2(src_path, dest)
    return dest


def _stringify_tags(tags):
    """tags 字段 → 入库用字符串（list/tuple 逗号拼接，dict 序列化为 JSON）。"""
    if isinstance(tags, str):
        return tags.strip()
    if isinstance(tags, (list, tuple, set)):
        return ",".join(str(t).strip() for t in tags if str(t).strip())
    if isinstance(tags, dict):
        return json.dumps(tags, ensure_ascii=False)
    return str(tags) if tags else ""


def _to_json_safe(value):
    """将任意 Python 对象收敛为 JSON 可序列化的基本类型。"""
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(text):
    """入库前文本清洗：去控制字符、统一换行、逐行去首尾空白、折叠连续空行。

    使 documents.content / document_chunks / 向量文本更干净精简（省存储、省 token）。
    """
    if not text:
        return ""
    t = _CTRL_RE.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n"))
    lines, blank = [], False
    for ln in t.split("\n"):
        s = ln.strip()
        if s:
            lines.append(s)
            blank = False
        elif not blank:
            lines.append("")
            blank = True
    return "\n".join(lines).strip()


def extract_pdf_text(file_path, parser=None):
    """尽力抽取 PDF 文本；无可用解析库或无文本时返回空字符串。

    决策（快 + 精简 + 兼容）：
      - pypdf 比 pdfplumber 快约一个量级，auto 模式默认只走 pypdf；
        仅在 pypdf 完全抽不出文本时自动回退 pdfplumber，避免慢解析器空转。
      - PyPDF2 是 pypdf 的废弃前身（同源），不再纳入回退链。
      - 逐页清洗并丢弃“近空白页”（纯图表/封面装饰页），减少噪音与 token。
    可选 KB_PDF_PARSER=pypdf|pdfplumber 强制指定解析器。
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"PDF 文件不存在: {file_path}")

    mode = (parser or PDF_PARSER).strip().lower() or "auto"
    if mode == "auto":
        candidates = ("pypdf", "pdfplumber")
    elif mode in ("pypdf", "pdfplumber"):
        candidates = (mode,)
    else:
        raise ValueError("KB_PDF_PARSER 只支持 auto / pypdf / pdfplumber")

    installed = [lib for lib in candidates if _lib_importable(lib)]
    if not installed:
        raise RuntimeError(
            "未安装 PDF 解析库。请执行: pip install pypdf 或 pip install pdfplumber"
        )

    last_err = None
    for lib in installed:
        try:
            text = _extract_pdf_text_with(file_path, lib)
        except Exception as e:          # 当前解析器失败 → 尝试下一个
            last_err = e
            print(f" PDF 解析器 {lib} 失败: {e}")
            continue
        if text:
            return text
    if last_err is not None:
        raise RuntimeError(f"PDF 全文抽取失败（{last_err}）")
    return ""


def _lib_importable(module):
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def _extract_pdf_text_with(file_path, lib):
    """用指定解析器抽取全文，逐页清洗；页间以空行分隔，近空白页直接丢弃。"""
    if lib == "pdfplumber":
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            raw_pages = [(page.extract_text() or "") for page in pdf.pages]
    else:
        import pypdf
        reader = pypdf.PdfReader(file_path)
        raw_pages = [(page.extract_text() or "") for page in reader.pages]

    pages = []
    for raw in raw_pages:
        page_text = clean_text(raw)
        if len(page_text) < MIN_PAGE_CHARS:      # 图表/封面等近空白页无检索价值
            continue
        pages.append(page_text)
    return "\n\n".join(pages)


# 文本分块
_SENT_ENDS = set("。！？；!?;")
_SOFT_ENDS = set("，,、：:…")


def _last_break(text, start, hi):
    """
    在 text[start:hi) 内查找“最靠近 hi”的断点下标（断在标点之后）。

    优先句子级标点（。！？；），其次短语级标点（，,、：），最后硬切到 hi。
    """
    for bounds in (_SENT_ENDS, _SOFT_ENDS):
        for i in range(hi - 1, start - 1, -1):
            if text[i] in bounds:
                return i + 1
    return hi


def chunk_text(text, chunk_size=None, overlap=None):
    """
    将长文本切分为带重叠的分块列表。

    策略：优先保持段落完整；当段落超过 chunk_size 时，
    在句读/标点边界处断开，避免把句子硬生生拦腰截断。

    默认 800/120，可用 KB_CHUNK_SIZE / KB_CHUNK_OVERLAP 覆盖（调整后需 rebuild 对账）。
    """
    if chunk_size is None:
        chunk_size = CHUNK_SIZE
    if overlap is None:
        overlap = CHUNK_OVERLAP
    text = (text or "").strip()
    if not text:
        return []
    try:
        chunk_size = int(chunk_size)
        overlap = int(overlap)
    except (TypeError, ValueError):
        chunk_size, overlap = 800, 120
    chunk_size = max(8, chunk_size)
    overlap = max(0, min(overlap, chunk_size - 1))

    paragraphs = [p.strip() for p in re.split(r"\n{1,3}", text) if p.strip()]

    chunks = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 1 <= chunk_size:
            current = (current + "\n" + para).strip()
            continue
        if current:
            chunks.append(current)
            current = current[-overlap:] if overlap else ""

        # 超长段落：在句读/标点边界切分，且相邻分块带 overlap
        while len(para) > chunk_size:
            cut = _last_break(para, 0, chunk_size)
            piece = para[:cut].strip()
            if piece:
                chunks.append(piece)
            next_start = cut - overlap
            if next_start <= 0:
                next_start = cut
            para = para[next_start:].strip()
            if not para:
                break
        if para:
            merged = (current + "\n" + para).strip()
            if current and len(merged) > chunk_size:
                # 余段并入会超限 → 先落盘 current，余段作为新块起点
                chunks.append(current)
                current = para
            else:
                current = merged

    if current:
        chunks.append(current)
    # 仅过滤空块；相邻分块本身设计为带 overlap，不去重
    return [c for c in chunks if c]


def _store_chunks_for(conn, document_id, content):
    """清空并重建某文档的全部分块写入 document_chunks 表（单事务批量写）。"""
    database.clear_chunks(conn, document_id)
    chunks = chunk_text(content)
    if chunks:
        database.add_chunks(conn, document_id, chunks)
    return len(chunks)


def _gen_file_id(prefix, name):
    """基于文件名生成稳定唯一 id。"""
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _save_news_json_backup(record, source_type="news"):
    """将新闻记录写为 data/raw/news/YYYY-MM-DD/<site>_<ts>[_seq].json 备份。"""
    target_dir = _date_folder("news")
    site = record.get("site_name", "news")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(target_dir, f"{site}_{timestamp}.json")
    seq = 1
    while os.path.exists(path):
        path = os.path.join(target_dir, f"{site}_{timestamp}_{seq}.json")
        seq += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


# 各来源入库
def ingest_news(json_data, vectorize=True):
    """
    接收一条新闻 dict/JSON 字符串。

    - 写入 documents 表（source_type='news'）
    - 仅当真正新入库时才写 JSON 备份到 data/raw/news/YYYY-MM-DD/
      （重复抓取直接跳过，不再产生冗余备份文件）
    - 建立文本分块，vectorize=True 时同步 upsert 到 Chroma 向量库

    返回 (inserted: bool, document_id: str, backup_path: str)
    """
    if isinstance(json_data, str):
        json_data = json.loads(json_data)
    if not isinstance(json_data, dict):
        raise ValueError("json_data 必须是 dict 或 JSON 字符串")

    cont_id = str(json_data.get("contId", "")).strip()
    title = str(json_data.get("title", "")).strip()
    body = clean_text(json_data.get("body") or json_data.get("content") or "")
    url = str(json_data.get("source_url") or json_data.get("url") or "").strip()

    if cont_id:
        doc_id = f"news_{cont_id}"
    else:
        digest = hashlib.md5(
            (title + json_data.get("body", "")).encode("utf-8")
        ).hexdigest()[:12]
        doc_id = f"news_{digest}"

    if not body:
        print(f" 正文为空，跳过入库: {doc_id}")
        return False, doc_id, ""

    tags = _stringify_tags(json_data.get("tags", "") or "")

    # 入库字段
    record = {
        "id": doc_id,
        "source_type": "news",
        "title": title,
        "authors": "",
        "abstract": "",
        "content": body,
        "url": url,
        "publish_date": _parse_news_date(json_data),
        "tags": tags,
        "file_path": "",   # JSON 备份路径在成功插入后回填
        "metadata": {
            "contId": cont_id,
            "site_name": json_data.get("site_name", ""),
            "firstPublishTime": json_data.get("firstPublishTime", ""),
            "date": json_data.get("date", ""),
            "crawled_at": json_data.get("crawled_at", ""),
            "images": _to_json_safe(json_data.get("images", [])),
        },
    }

    # 图库元数据清洗为可序列化 JSON（用于 raw 目录备份）
    rec_copy = {
        "title": title,
        "date": json_data.get("date", ""),
        "body": body,
        "contId": cont_id,
        "firstPublishTime": json_data.get("firstPublishTime", ""),
        "source_url": url,
        "site_name": json_data.get("site_name", ""),
        "crawled_at": json_data.get("crawled_at", ""),
        "tags": tags,
    }
    if json_data.get("images"):
        rec_copy["images"] = _to_json_safe(json_data.get("images", []))

    conn = database.get_connection()
    backup_path = ""
    try:
        inserted, row = database.insert_document(conn, record)
        if inserted:
            try:
                backup_path = _save_news_json_backup(rec_copy)
                conn.execute(
                    "UPDATE documents SET file_path = ? WHERE id = ?",
                    (backup_path, doc_id),
                )
                conn.commit()
            except OSError as e:
                print(f" JSON 备份写盘失败（不影响入库）: {e}")
                backup_path = ""
            n_chunks = _store_chunks_for(conn, doc_id, body)
            print(f" 新闻已入库 {doc_id}（分块 {n_chunks} 个）")
        else:
            print(f" 新闻已存在，跳过: {doc_id}")
        if vectorize:
            _try_vectorize_document(
                conn, doc_id, body, title=title, source_type="news"
            )
    finally:
        conn.close()
    return inserted, doc_id, backup_path


def _ingest_pdf_doc(source_type, prefix, file_path, metadata=None, vectorize=True):
    """
    论文/教材/研报 PDF 统一入库（source_type ∈ paper/textbook/report）。

    - 文档 id：优先取 metadata['id']，否则按“类型前缀 + 文件名摘要”稳定生成，
      保证同名文件重复上传命中同一 doc_id 从而跳过（不重复抽取/归档）。
    - 归档：复制原文件到 data/raw/<papers|textbooks|reports>/
    - 抽取全文写入 documents.content，按段落/句读分块写 document_chunks
    - vectorize=True 时增量 upsert 到 Chroma（元数据含 title / source_type）

    返回 (inserted, document_id, raw_path)
    """
    if source_type not in SOURCE_DIRS:
        raise ValueError(
            f"source_type 必须是 {tuple(SOURCE_DIRS)} 之一，收到: {source_type!r}"
        )
    if not file_path or not os.path.exists(file_path):
        raise FileNotFoundError(f"PDF 文件不存在: {file_path}")
    metadata = metadata or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata 必须是 dict")

    name = os.path.basename(file_path)
    stem = os.path.splitext(name)[0]
    doc_id = str(metadata.get("id") or "").strip() or _gen_file_id(prefix, name)

    record = {
        "id": doc_id,
        "source_type": source_type,
        "title": str(metadata.get("title") or stem).strip() or stem,
        "authors": str(metadata.get("authors") or "").strip(),
        "abstract": str(
            metadata.get("abstract") or metadata.get("summary") or ""
        ).strip(),
        "content": "",
        "url": str(metadata.get("url") or "").strip(),
        "publish_date": str(
            metadata.get("publish_date") or metadata.get("date") or ""
        ).strip(),
        "tags": _stringify_tags(metadata.get("tags", "") or ""),
        "file_path": "",
        "metadata": {},
    }

    conn = database.get_connection()
    try:
        exists = database.document_exists(conn, doc_id)
    finally:
        conn.close()
    if exists:
        print(f" 文档已存在，跳过: {doc_id}")
        return False, doc_id, ""

    # 归档原始文件
    raw_path = _copy_to_raw(source_type, file_path, extra_name=name)
    record["file_path"] = raw_path

    try:
        content = extract_pdf_text(file_path)
    except Exception as e:
        print(f" 全文抽取失败: {e}")
        raise
    if content and content.strip():
        record["content"] = content
    else:
        print(f" 未能抽取到文本（可能为扫描件），仅入库元数据: {doc_id}")

    extra = {
        k: v for k, v in metadata.items()
        if k not in ("id", "title", "authors", "abstract", "summary",
                     "url", "publish_date", "date", "tags", "file_path")
    }
    record["metadata"] = _to_json_safe(extra)

    conn = database.get_connection()
    try:
        inserted, row = database.insert_document(conn, record)
        if inserted:
            n_chunks = _store_chunks_for(conn, doc_id, record["content"])
            print(f" {source_type} 已入库 {doc_id}（分块 {n_chunks} 个）")
        else:
            print(f" {source_type} 已存在，跳过: {doc_id}")
        if vectorize and record["content"] and record["content"].strip():
            _try_vectorize_document(
                conn, doc_id, record["content"],
                title=record["title"], source_type=source_type,
            )
    finally:
        conn.close()
    return inserted, doc_id, raw_path


def ingest_paper(file_path, metadata=None, vectorize=True):
    """接收 PDF 路径与元数据，入库 source_type='paper'。"""
    return _ingest_pdf_doc("paper", "paper", file_path, metadata, vectorize)


def ingest_textbook(file_path, metadata=None, vectorize=True):
    """接收 PDF 路径与元数据，入库 source_type='textbook'。"""
    return _ingest_pdf_doc("textbook", "textbook", file_path, metadata, vectorize)


def ingest_report(file_path, metadata=None, vectorize=True):
    """接收 PDF 路径与元数据，入库 source_type='report'。"""
    return _ingest_pdf_doc("report", "report", file_path, metadata, vectorize)


# 增量向量化核心函数
_EMBEDDING_FN = None
_EMBEDDING_FN_FAILED = False
_CHROMA_CLIENT = None


def _default_embedding_fn():
    """
    构建统一 embedding function（离线 sentence-transformers all-MiniLM-L6-v2）。

    - 全项目所有读写 Chroma 的地方都经此函数取同一 EF，保证 add/query 一致；
    - 模型权重已缓存于 ~/.cache/huggingface，构造后强制 HF_HUB_OFFLINE=1，
      不会像 Chroma 默认 ONNX 那样联网反复下载卡顿；
    - 首次调用即预热一次，把模型缺失/加载失败提前暴露（打印 并返回 None），
      而不是拖到第一条数据 upsert 才失败。
    """
    global _EMBEDDING_FN, _EMBEDDING_FN_FAILED
    if _EMBEDDING_FN is not None:
        return _EMBEDDING_FN
    if _EMBEDDING_FN_FAILED:
        return None
    try:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from chromadb.utils.embedding_functions import (
            SentenceTransformerEmbeddingFunction,
        )
        fn = SentenceTransformerEmbeddingFunction(
            model_name=KB_EMBEDDING_MODEL,
            device="cpu",
        )
        fn(["__warmup__"])  # 预热，提前暴露加载错误
        _EMBEDDING_FN = fn
        print(f" 向量模型就绪: {KB_EMBEDDING_MODEL}（离线，384 维）")
    except Exception as e:
        _EMBEDDING_FN_FAILED = True
        print(f" 离线 embedding 不可用，退回 Chroma 默认: {e}")
        return None
    return _EMBEDDING_FN


def _get_chroma_client():
    """获取 Chroma PersistentClient（**进程内单例**）；未安装 chromadb 时抛 RuntimeError。

    单例化的理由：PersistentClient 由 Rust 实现，每次构造都要重付一遍客户端初始化
    开销；实测临时库新建 5 个客户端 → RSS +8MB，且这部分内存不会在 GC 时归还给
    进程。而长驻守护（news_scheduler）每天会走到本函数约 800 次（每窗口每来源一次），
    完全没必要反复构造。单例化后连续 10 次调用 RSS 与线程数均为零增长（实测）。
    与 _default_embedding_fn 的缓存策略保持一致（同进程同库同 EF，语义不变）。

    说明（2026-09-17）：本改动是为缓解观察到的守护进程膨胀
    （运行 2d11h：VmRSS 682MB / VmHWM 1.18GB / VmSwap 357MB / 177 线程，其中
    tokio-rt-worker 64 + 未命名 python 线程 110；同期 fin-tool-server 仅 9.9MB）。
    但需注意：探针实验**未**证实线程来自客户端构造（新建 5 个客户端线程数无变化），
    177 线程的实际来源尚未定位，更可能在 embedding（torch/ONNX）路径上。此处只消除
    已证实的重复构造开销，不宣称已解决线程膨胀。
    """
    global _CHROMA_CLIENT
    if _CHROMA_CLIENT is not None:
        return _CHROMA_CLIENT
    try:
        import chromadb
    except ImportError:
        raise RuntimeError(
            "未安装 chromadb，向量检索不可用。请执行: pip install chromadb"
        )
    os.makedirs(CHROMA_DIR, exist_ok=True)
    _CHROMA_CLIENT = chromadb.PersistentClient(path=CHROMA_DIR)
    return _CHROMA_CLIENT


def _resolve_collection_ef(client, collection_name, fn, allow_recreate=False):
    """创建/获取集合，并处理「旧集合未绑定自定义 EF」的兼容迁移。

    Chroma 2.x 起会把集合的 embedding_function 配置持久化；早前以
    “不传 EF”方式建过的集合被固化为 default，之后再带自定义 EF
    （如 sentence-transformers）访问即抛 "embedding function conflict"。
    此处统一处理：
      - allow_recreate=True（rebuild 全量重建）：删旧集合重建，代价可接受；
      - 旧集合为空：无向量可丢，安全删除重建（幂等迁移）；
      - 旧集合非空且非 rebuild：不自动删除，抛出明确指引，避免误删已有向量。
    """
    kwargs = {}
    if fn is not None:
        kwargs["embedding_function"] = fn
    try:
        return client.get_or_create_collection(name=collection_name, **kwargs)
    except ValueError as e:
        if "embedding function" not in str(e).lower():
            raise
        legacy_count = -1
        try:
            legacy = client.get_collection(name=collection_name)
            legacy_count = legacy.count() if legacy is not None else 0
        except Exception:
            pass
        if not (allow_recreate or legacy_count == 0):
            raise ValueError(
                "Chroma 集合已绑定 default EF 且含非空向量，无法原地切换嵌入模型。"
                "请先执行一次 get_or_create_vector_index(rebuild=True) 重建迁移，"
                "避免误删已有向量。"
            ) from e
        if legacy_count > 0:
            print(" 集合 embedding 配置不一致（default EF），重建场景：清空旧集合")
        try:
            client.delete_collection(name=collection_name)
        except Exception:
            pass
        print(" 旧集合未绑定自定义 EF，已删除并重新绑定统一离线 EF")
        return client.get_or_create_collection(name=collection_name, **kwargs)


def _get_collection(collection_name="knowledge_docs", embedding_fn=None):
    """轻量获取集合（不触发全库重建），默认绑定统一离线 EF。"""
    try:
        client = _get_chroma_client()
        fn = embedding_fn if embedding_fn is not None else _default_embedding_fn()
        return _resolve_collection_ef(client, collection_name, fn)
    except Exception as e:
        print(f" 获取集合失败: {e}")
        return None


def _upsert_doc_vectors(doc_id, chunks, title="", source_type="",
                        collection=None, purge_existing=True):
    """
    将单个文档的分块 upsert 到 Chroma（增量更新，只影响当前文档）。

    - 元数据与全量重建保持同构（title / source_type / chunk_index）
    - 默认先删除该文档的旧向量，避免内容变化后残留过期分块（脏数据）
    """
    if not chunks:
        return 0
    if collection is None:
        collection = _get_collection()
        if collection is None:
            return 0

    ids = [f"{doc_id}__{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "document_id": doc_id,
            "title": title,
            "source_type": source_type,
            "chunk_index": i,
        }
        for i in range(len(chunks))
    ]
    try:
        if purge_existing:
            try:
                old = collection.get(where={"document_id": doc_id}, include=[])
            except Exception:
                old = collection.get(where={"document_id": doc_id})
            old_ids = (old or {}).get("ids") or []
            if old_ids:
                collection.delete(ids=old_ids)
        collection.upsert(ids=ids, documents=chunks, metadatas=metadatas)
        return len(chunks)
    except Exception as e:
        print(f" upsert 失败 {doc_id}: {e}")
        return 0


def _content_digest(content):
    """文档正文的稳定摘要（UTF-8 md5）。用作增量对账的内容指纹。"""
    return hashlib.md5((content or "").encode("utf-8")).hexdigest()


def _index_version(content, chunk_count):
    """某文档当前应达到的向量化版本（与 database.document_index_state 字段同构）。

    版本指纹 = content_hash + chunk_count + 分块参数 + 嵌入模型。
    只有指纹全部一致才认为「已同步」；任一变化都触发该文档的单篇重建。
    """
    return {
        "content_hash": _content_digest(content),
        "chunk_count": int(chunk_count or 0),
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "embed_model": KB_EMBEDDING_MODEL,
    }


def _state_matches(state, version):
    """状态行与目标版本是否完全一致（state 为 None 视为不一致）。"""
    if state is None:
        return False
    return all(state[k] == v for k, v in version.items())


def _record_index_state(conn, doc_id, version):
    """Chroma upsert 成功后将版本指纹落库（崩溃安全：先向量后状态）。"""
    database.set_index_state(
        conn, doc_id,
        content_hash=version["content_hash"],
        chunk_count=version["chunk_count"],
        chunk_size=version["chunk_size"],
        chunk_overlap=version["chunk_overlap"],
        embed_model=version["embed_model"],
    )


def _try_vectorize_document(conn, doc_id, content, title="", source_type="", force=False):
    """
    单文档向量化（增量）：仅 upsert 当前文档的分块，不触发全库重建。

    - content 未变（document_index_state 指纹命中）时直接跳过，避免重复删加；
    - 内容变化/首次入库/上次失败（无状态）时自动重建该单篇；
    - title / source_type 缺省时自动从 documents 表补取。
    """
    content = (content or "").strip()
    if not content:
        return
    if not title or not source_type:
        try:
            row = conn.execute(
                "SELECT title, source_type FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            title = title or row["title"] or ""
            source_type = source_type or row["source_type"] or ""

    chunks = chunk_text(content)
    if not chunks:
        return
    version = _index_version(content, len(chunks))
    if not force and _state_matches(database.get_index_state(conn, doc_id), version):
        return  # 已同步，跳过向量化（内容未变化）
    count = _upsert_doc_vectors(
        doc_id, chunks, title=title, source_type=source_type
    )
    if count:
        _record_index_state(conn, doc_id, version)
        print(f" 已增量向量化 {doc_id}（{count} 分块）")
    else:
        print(f" 增量向量化失败 {doc_id}")


def _prune_orphan_vectors(collection, valid_doc_ids):
    """删除 Chroma 中已不存在于 SQLite 的孤儿向量；单次元数据扫描。"""
    try:
        if collection is None or collection.count() == 0:
            return 0
        existing = collection.get(include=["metadatas"])
        ids = existing.get("ids") or []
        metas = existing.get("metadatas") or []
    except Exception as e:
        print(f" 孤儿向量扫描失败: {e}")
        return 0
    orphan = [cid for cid, m in zip(ids, metas)
              if (m or {}).get("document_id", "") not in valid_doc_ids]
    if not orphan:
        return 0
    try:
        collection.delete(ids=orphan)
    except Exception as e:
        print(f" 孤儿向量清理失败: {e}")
        return 0
    print(f" 已清理 {len(orphan)} 条孤儿向量（文档已不在知识库）")
    return len(orphan)


def _reindex_doc(conn, collection, doc, purge_existing=True):
    """单篇对账：刷新分块表 → upsert Chroma → 落状态指纹。返回写入分块数。"""
    content = (doc.get("content") or "").strip()
    if not content:
        _record_index_state(conn, doc["id"], _index_version("", 0))
        return 0
    chunks = chunk_text(content)
    if not chunks:
        _record_index_state(conn, doc["id"], _index_version("", 0))
        return 0
    _store_chunks_for(conn, doc["id"], content)
    count = _upsert_doc_vectors(
        doc["id"], chunks, title=doc.get("title") or "",
        source_type=doc.get("source_type") or "", collection=collection,
        purge_existing=purge_existing,
    )
    if count:
        _record_index_state(conn, doc["id"], _index_version(content, len(chunks)))
    else:
        print(f" 向量化失败 [{doc['id']}]")
    return count


def get_or_create_vector_index(embedding_fn=None, rebuild=False, collection=None):
    """连接/创建 Chroma 集合并按 document_index_state 增量对账。

    rebuild=False：只重建「新增 / 内容或分块参数或模型变化 / 上次失败」的文档单篇；
    - 状态表为空时先 bootstrap：一次元数据扫描清理孤儿向量、核对已同步文档并
      回填状态（不重新编码），只重嵌真正缺失/不一致部分 —— 不再出现「检索即
      全库重建」。本函数的对账职责与查询完全解耦。
    rebuild=True：先整体清空集合再全量重建（换模型 / 调分块参数后使用）。
    日常检索请使用 _get_collection()，不要调用本函数。
    """
    if collection is None:
        try:
            client = _get_chroma_client()
        except RuntimeError as e:
            print(f" {e}")
            return None
        fn = embedding_fn if embedding_fn is not None else _default_embedding_fn()
        collection = _resolve_collection_ef(client, "knowledge_docs", fn,
                                            allow_recreate=rebuild)

    if rebuild:
        try:
            existing = collection.get(include=[])
            if existing and existing.get("ids"):
                collection.delete(ids=existing["ids"])
                print(" 已清空旧向量索引，开始重建")
        except Exception as e:
            print(f" 清空索引失败（继续增量重建）: {e}")

    conn = database.get_connection()
    try:
        doc_rows = [dict(r) for r in conn.execute(
            "SELECT id, title, source_type, content FROM documents"
        ).fetchall()]
        if not doc_rows:
            database.clear_index_states(conn)
            return collection

        if rebuild:
            database.clear_index_states(conn)

        state_total = database.count_index_states(conn)
        if state_total > 0 and not rebuild and collection.count() == 0:
            print(" 集合为空但状态表有记录：清空状态走全量重建")
            database.clear_index_states(conn)
            state_total = 0

        bootstrap = state_total == 0 and not rebuild
        per_doc_vectors = {}
        if bootstrap:
            valid = {d["id"] for d in doc_rows}
            pruned = _prune_orphan_vectors(collection, valid)
            try:
                existing = collection.get(include=["metadatas"])
            except Exception:
                existing = {}
            for m in existing.get("metadatas") or []:
                did = (m or {}).get("document_id", "")
                per_doc_vectors[did] = per_doc_vectors.get(did, 0) + 1
            print(f" bootstrap: 元数据对齐（向量 {sum(per_doc_vectors.values())}，"
                  f"孤儿 {pruned}）")

        state_map = {s["doc_id"]: s for s in conn.execute(
            "SELECT * FROM document_index_state"
        ).fetchall()}

        changed, matched = [], 0
        for doc in doc_rows:
            content = (doc.get("content") or "").strip()
            chunks = chunk_text(content)
            version = _index_version(content, len(chunks))
            if bootstrap:
                if per_doc_vectors.get(doc["id"], 0) == len(chunks):
                    _record_index_state(conn, doc["id"], version)
                    matched += 1
                else:
                    changed.append(doc)
                continue
            if _state_matches(state_map.get(doc["id"]), version):
                matched += 1
            else:
                changed.append(doc)

        total = 0
        failures = 0
        for doc in changed:
            count = _reindex_doc(conn, collection, doc, purge_existing=not rebuild)
            total += count
            if not count:
                failures += 1
    finally:
        conn.close()

    print(f" 向量索引进度: 已同步 {matched}，本次写入 {total} 分块"
          f"（chroma 共 {collection.count()}）" + (f"，失败 {failures}" if failures else ""))
    return collection

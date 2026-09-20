"""database.py — 统一知识库数据库结构"""

import os
import sqlite3
import json
from datetime import datetime

DB_HOME = "/mnt/d/数据库"


def storage_resolve(name, d_parts, local):
    """存储路径解析顺序：环境变量 → KB_STORE_ROOT / D 盘“数据库”母目录 → 本地相对路径。"""
    val = os.environ.get(name)
    if val:
        return val
    root = os.environ.get("KB_STORE_ROOT") or DB_HOME
    if os.path.isdir(root):
        return os.path.join(root, *d_parts)
    return local


# 默认数据库位置
DEFAULT_DB_PATH = storage_resolve(
    "KB_DB_PATH", ("主数据库", "knowledge.db"), os.path.join("data", "knowledge.db"))

# 允许的来源类型
VALID_SOURCE_TYPES = ("news", "paper", "textbook", "report")


def get_db_path():
    """返回数据库文件路径（支持 KB_DB_PATH 环境变量覆盖）。"""
    return os.environ.get("KB_DB_PATH", DEFAULT_DB_PATH)


def get_connection(db_path=None):
    """获取 SQLite 连接，自动确保父目录存在。"""
    db_path = db_path or get_db_path()
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# 建表
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,              -- 格式: 来源_唯一标识, 如 news_12345
    source_type  TEXT NOT NULL,                 -- news / paper / textbook / report
    title        TEXT NOT NULL,
    authors      TEXT DEFAULT '',               -- 可为空
    abstract     TEXT DEFAULT '',               -- 摘要或简介
    content      TEXT DEFAULT '',               -- 全文或详细内容
    url          TEXT DEFAULT '',
    publish_date TEXT DEFAULT '',               -- 发布日期
    tags         TEXT DEFAULT '',               -- 逗号分隔关键词
    file_path    TEXT DEFAULT '',               -- 本地文件位置（PDF 等）
    metadata     TEXT DEFAULT '{}',             -- JSON 格式来源特有字段
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content     TEXT NOT NULL,
    embedding   BLOB,                           -- 向量数据（预留）
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_documents_source_type
    ON documents(source_type);

CREATE INDEX IF NOT EXISTS idx_documents_publish_date
    ON documents(publish_date);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id
    ON document_chunks(document_id);

-- 向量化状态表：记录每个文档最后一次成功向量化时的版本指纹。
-- SQLite 是对账真源；Chroma 仅作为向量仓库，不再承担“是否已同步”的判定。
-- 版本指纹 = content_hash : chunk_count : chunk_size : chunk_overlap : embed_model，
-- 任一变化（内容更新 / 分块参数调整 / 换嵌入模型）都会触发该文档的单篇重建。
CREATE TABLE IF NOT EXISTS document_index_state (
    doc_id        TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    chunk_count   INTEGER NOT NULL,
    chunk_size    INTEGER NOT NULL,
    chunk_overlap INTEGER NOT NULL,
    embed_model   TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
);
"""

QUANT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,               -- 统一代码: 600519.SH / AAPL / ^GSPC
    source      TEXT NOT NULL,               -- akshare/tushare/alpha_vantage/baostock/yfinance
    trade_date  TEXT NOT NULL,               -- YYYY-MM-DD
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,                        -- 股/手：随源口径
    amount      REAL,                        -- 成交额（元），随源口径
    adj_close   REAL,
    extra       TEXT DEFAULT '{}',           -- 源特有字段 JSON
    updated_at  TEXT NOT NULL,
    UNIQUE (symbol, source, trade_date)
);

CREATE TABLE IF NOT EXISTS stock_fundamental (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT NOT NULL,
    source         TEXT NOT NULL,
    trade_date     TEXT NOT NULL,            -- 指标对应日期 YYYY-MM-DD
    pe_ttm         REAL,
    pe             REAL,
    pb             REAL,
    ps_ttm         REAL,
    market_cap     REAL,                     -- 总市值（元）
    float_market_cap REAL,                   -- 流通市值（元）
    dividend_yield REAL,
    total_share    REAL,
    float_share    REAL,
    extra          TEXT DEFAULT '{}',
    updated_at     TEXT NOT NULL,
    UNIQUE (symbol, source, trade_date)
);

CREATE TABLE IF NOT EXISTS stock_financial (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol             TEXT NOT NULL,
    source             TEXT NOT NULL,
    report_date        TEXT NOT NULL,        -- 报告期 YYYY-MM-DD（如 2025-06-30）
    revenue            REAL,                 -- 营业总收入（元）
    net_profit         REAL,                 -- 归母净利润（元）
    roe                REAL,
    roa                REAL,
    gross_margin       REAL,
    net_margin         REAL,
    eps                REAL,
    bps                REAL,
    debt_ratio         REAL,
    operating_cashflow REAL,
    extra              TEXT DEFAULT '{}',
    updated_at         TEXT NOT NULL,
    UNIQUE (symbol, source, report_date)
);

CREATE TABLE IF NOT EXISTS data_sync_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    task          TEXT NOT NULL,             -- daily/fundamental/financial
    symbol        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,             -- ok / error / skipped
    rows_affected INTEGER DEFAULT 0,
    message       TEXT DEFAULT '',
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_daily_symbol_date ON stock_daily(symbol, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_fund_symbol_date  ON stock_fundamental(symbol, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_fin_symbol_date   ON stock_financial(symbol, report_date DESC);
"""

MARKET_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ticker_universe (
    symbol     TEXT PRIMARY KEY,             -- 统一代码: 600519.SH
    name       TEXT NOT NULL,                -- 官方简称: 贵州茅台
    exchange   TEXT DEFAULT '',              -- SH / SZ / BJ
    market     TEXT DEFAULT 'cn',            -- cn / hk / us
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_universe_name ON ticker_universe(name);

CREATE TABLE IF NOT EXISTS market_snapshot (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,                -- YYYY-MM-DD
    slot       TEXT NOT NULL,                -- 快照时段标签: 09:25 / 11:30 / 15:00
    kind       TEXT NOT NULL,                -- index / industry_board / concept_board
    code       TEXT NOT NULL,                -- 指数代码或板块代码
    name       TEXT NOT NULL,                -- 指数/板块名称
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,                         -- 快照时点最新点位/价格
    pct_chg    REAL,                         -- 涨跌幅 %
    volume     REAL,
    amount     REAL,                         -- 成交额（元）
    extra      TEXT DEFAULT '{}',            -- 源特有字段 JSON
    updated_at TEXT NOT NULL,
    UNIQUE (trade_date, slot, kind, code)
);
CREATE INDEX IF NOT EXISTS idx_snap_datetime ON market_snapshot(trade_date, slot, kind);

CREATE TABLE IF NOT EXISTS news_tickers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id    TEXT NOT NULL,                -- 对应 documents.id
    symbol     TEXT NOT NULL,                -- 统一代码: 600519.SH
    name       TEXT DEFAULT '',              -- 命中的公司简称
    method     TEXT DEFAULT 'rules',         -- rules / model / manual
    matched_in TEXT DEFAULT 'title',         -- title / content
    confidence REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    UNIQUE (news_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_nt_symbol   ON news_tickers(symbol);
CREATE INDEX IF NOT EXISTS idx_nt_news     ON news_tickers(news_id);
CREATE INDEX IF NOT EXISTS idx_nt_created  ON news_tickers(created_at);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    news_id          TEXT NOT NULL,          -- 对应 documents.id
    symbol           TEXT NOT NULL,          -- 统一代码
    horizon_days     INTEGER NOT NULL,       -- 持有 N 个交易日
    direction        TEXT DEFAULT 'neutral', -- up / down / neutral
    direction_method TEXT DEFAULT 'lexicon', -- lexicon / model / manual
    signal_date      TEXT NOT NULL,          -- 信号归属日 = publish_date
    signal_time      TEXT DEFAULT '',        -- 本地 HH:MM:SS（无则空）
    entry_date       TEXT,                   -- 建仓日（收盘计）
    entry_price      REAL,
    exit_date        TEXT,
    exit_price       REAL,
    symbol_pct       REAL,                   -- 标的区间涨跌幅 %
    bench_symbol     TEXT DEFAULT '000300.SH',
    bench_pct        REAL,                   -- 同区间沪深300涨跌幅 %
    excess_pct       REAL,                   -- 超额收益 % = symbol_pct - bench_pct
    status           TEXT DEFAULT 'pending', -- pending / filled / nodata / error
    note             TEXT DEFAULT '',
    updated_at       TEXT NOT NULL,
    UNIQUE (news_id, symbol, horizon_days)
);
CREATE INDEX IF NOT EXISTS idx_so_pending ON signal_outcomes(status, signal_date);
CREATE INDEX IF NOT EXISTS idx_so_symbol  ON signal_outcomes(symbol, entry_date);

-- 需求 3：主题预测 → 收盘回填 → LLM 反思（与上述各表共存于同一 SQLite）
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL,          -- 预测目标交易日 YYYY-MM-DD
    basis_date      TEXT,                   -- 依据日（昨日板块映射所在交易日）
    side            TEXT NOT NULL,          -- leading 领涨 / risk 风险
    seq             INTEGER NOT NULL DEFAULT 1,
    theme           TEXT NOT NULL,          -- 板块/概念名称（取自定义候选名，便于结算对名）
    kind            TEXT DEFAULT '',        -- industry_board / concept_board / index
    board_code      TEXT DEFAULT '',
    rationale       TEXT DEFAULT '',        -- 预测依据（模型输出 / 规则说明）
    vs_outcome      TEXT DEFAULT '{}',      -- 需求3：收盘回填 JSON（matched/hit/pct/rank…）
    status          TEXT DEFAULT 'pending', -- pending / evaluated / missed
                                            -- missed: 整日未结算（见 predict._mark_pending_missed）
    params_fp       TEXT DEFAULT '',        -- 生成时生效参数指纹
    params_snapshot TEXT DEFAULT '{}',      -- 生成时参数快照（回滚/复现依据）
    model           TEXT DEFAULT 'rules',   -- deepseek / rules
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (trade_date, side, seq)
);
CREATE INDEX IF NOT EXISTS idx_pred_trade ON predictions(trade_date, status);

CREATE TABLE IF NOT EXISTS predict_days (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date      TEXT NOT NULL UNIQUE,   -- 预测目标交易日
    basis_date      TEXT,
    generated_at    TEXT,
    settled_at      TEXT,
    model           TEXT DEFAULT '',
    params_fp       TEXT DEFAULT '',
    params_snapshot TEXT DEFAULT '{}',
    summary         TEXT DEFAULT '{}',      -- 当日命中/错判/漏判 复盘维度 JSON
    status          TEXT DEFAULT 'generated', -- generated / settled / missed
                                            -- missed: 整日未生成（守护宕机等），
                                            -- 由 predict._ensure_day_missed 补台账行
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predict_params (
    key         TEXT PRIMARY KEY,           -- params 点路径，如 generation.n_leading
    value       TEXT NOT NULL,              -- JSON 标量
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predict_param_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fp          TEXT NOT NULL,              -- 该历史态参数指纹（前16位sha256）
    parent_fp   TEXT DEFAULT '',            -- 上一状态指纹（回滚链）
    params      TEXT NOT NULL,              -- 完整 params 快照 JSON
    reason      TEXT DEFAULT '',
    source      TEXT DEFAULT 'manual',      -- manual / reflect / rollback / auto
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pph_fp ON predict_param_history(fp);

CREATE TABLE IF NOT EXISTS predict_reflections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date  TEXT NOT NULL,              -- 复盘生成日
    window_days INTEGER,
    params_fp   TEXT DEFAULT '',            -- 复盘时的生效参数指纹
    summary     TEXT DEFAULT '{}',          -- 反思维度 JSON（命中/错判/漏判 + LLM 评述）
    suggestions TEXT DEFAULT '[]',          -- LLM 建议参数变更 JSON
    applied     TEXT DEFAULT '{}',          -- 实际应用摘要 {applied, skipped}
    model       TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pref_date ON predict_reflections(trade_date);

-- 数据补齐（2026-09-11）：全市场个股快照与板块成分
CREATE TABLE IF NOT EXISTS stock_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date  TEXT NOT NULL,             -- YYYY-MM-DD
    slot        TEXT NOT NULL,             -- 快照时点标签，默认 15:00
    code        TEXT NOT NULL,             -- 6 位代码
    symbol      TEXT NOT NULL,             -- 统一代码 600519.SH
    name        TEXT NOT NULL,             -- 证券简称
    close       REAL,
    pct_chg     REAL,                      -- 涨跌幅 %
    open        REAL,
    high        REAL,
    low         REAL,
    prev_close  REAL,
    volume      REAL,                      -- 成交量（手）
    amount      REAL,                      -- 成交额（元）
    turnover    REAL,                      -- 换手率 %
    total_cap   REAL,                      -- 总市值（元）
    float_cap   REAL,                      -- 流通市值（元）
    pe          REAL,
    pb          REAL,
    source      TEXT DEFAULT 'em',
    updated_at  TEXT NOT NULL,
    UNIQUE (trade_date, slot, code)
);
CREATE INDEX IF NOT EXISTS idx_ss_date ON stock_snapshot(trade_date, slot);
CREATE INDEX IF NOT EXISTS idx_ss_code ON stock_snapshot(code);

CREATE TABLE IF NOT EXISTS board_members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    board_kind  TEXT NOT NULL,             -- industry_board / concept_board
    board_code  TEXT NOT NULL,             -- BKxxxx
    board_name  TEXT NOT NULL,
    symbol      TEXT NOT NULL,             -- 统一代码
    stock_name  TEXT DEFAULT '',
    source      TEXT DEFAULT 'em',
    updated_at  TEXT NOT NULL,
    UNIQUE (board_kind, board_code, symbol)
);
CREATE INDEX IF NOT EXISTS idx_bm_board  ON board_members(board_kind, board_code);
CREATE INDEX IF NOT EXISTS idx_bm_symbol ON board_members(symbol);
"""


def init_db(db_path=None):
    """
    初始化数据库：创建 documents / document_chunks 表及索引。
    重复调用是安全的。
    """
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(QUANT_SCHEMA_SQL)
        conn.executescript(MARKET_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    return db_path or get_db_path()


def ensure_quant_schema(db_path=None):
    """仅创建量化/基本面表（不重复建旧表）；幂等，供数据接入模块单独调用。"""
    conn = get_connection(db_path)
    try:
        conn.executescript(QUANT_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    return db_path or get_db_path()


def ensure_market_schema(db_path=None):
    """仅创建需求2表（行情快照/标的关联/信号回测）；幂等。"""
    conn = get_connection(db_path)
    try:
        conn.executescript(MARKET_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    return db_path or get_db_path()


# documents 表操作
def _normalize_document(doc):
    """将传入字典规整为入库字段，缺失字段补默认值。"""
    if doc.get("source_type") not in VALID_SOURCE_TYPES:
        raise ValueError(
            f"source_type 必须是 {VALID_SOURCE_TYPES} 之一，"
            f"收到: {doc.get('source_type')!r}"
        )
    if not doc.get("id"):
        raise ValueError("id 为必填字段")

    metadata = doc.get("metadata") or {}
    if not isinstance(metadata, str):
        metadata = json.dumps(metadata, ensure_ascii=False)

    return {
        "id": doc["id"],
        "source_type": doc["source_type"],
        "title": doc.get("title", ""),
        "authors": doc.get("authors", "") or "",
        "abstract": doc.get("abstract", "") or "",
        "content": doc.get("content", "") or "",
        "url": doc.get("url", "") or "",
        "publish_date": doc.get("publish_date", "") or "",
        "tags": doc.get("tags", "") or "",
        "file_path": doc.get("file_path", "") or "",
        "metadata": metadata,
        "created_at": doc.get("created_at", ""),
    }


def insert_document(conn, doc, replace=False):
    """
    插入一条文档记录。

    返回 (inserted, row)
      inserted: bool  该 id 是本次新插入（True）还是已存在（False）
    """
    if not doc.get("created_at"):
        doc["created_at"] = datetime.now().isoformat()

    if isinstance(doc.get("metadata"), (dict, list)):
        doc["metadata"] = json.dumps(doc["metadata"], ensure_ascii=False)

    row = _normalize_document(doc)

    existing = conn.execute(
        "SELECT id FROM documents WHERE id = ?", (row["id"],)
    ).fetchone()

    if replace:
        conn.execute(
            """INSERT OR REPLACE INTO documents
               (id, source_type, title, authors, abstract, content,
                url, publish_date, tags, file_path, metadata, created_at)
               VALUES
               (:id, :source_type, :title, :authors, :abstract, :content,
                :url, :publish_date, :tags, :file_path, :metadata, :created_at)""",
            row,
        )
        conn.commit()
        return (True, dict(row))

    if existing:
        return (False, get_document(conn, row["id"]))

    conn.execute(
        """INSERT INTO documents
           (id, source_type, title, authors, abstract, content,
            url, publish_date, tags, file_path, metadata, created_at)
           VALUES
           (:id, :source_type, :title, :authors, :abstract, :content,
            :url, :publish_date, :tags, :file_path, :metadata, :created_at)""",
        row,
    )
    conn.commit()
    return (True, dict(row))


def get_document(conn, doc_id):
    """按 id 返回单条文档（dict），不存在返回 None。"""
    cur = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (doc_id,)
    )
    row = cur.fetchone()
    return dict(row) if row else None


def document_exists(conn, doc_id):
    return conn.execute(
        "SELECT 1 FROM documents WHERE id = ?", (doc_id,)
    ).fetchone() is not None


def _index_state_row(row):
    """sqlite3.Row → dict（无记录返回 None）。"""
    return dict(row) if row is not None else None


def get_index_state(conn, doc_id):
    """读取某文档的向量化状态指纹，无记录返回 None。"""
    return _index_state_row(conn.execute(
        "SELECT * FROM document_index_state WHERE doc_id = ?", (doc_id,)
    ).fetchone())


def set_index_state(conn, doc_id, content_hash, chunk_count,
                    chunk_size, chunk_overlap, embed_model):
    """写入/更新某文档的向量化状态（应在 Chroma upsert 成功之后调用）。"""
    conn.execute(
        """INSERT INTO document_index_state
           (doc_id, content_hash, chunk_count, chunk_size,
            chunk_overlap, embed_model, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(doc_id) DO UPDATE SET
               content_hash  = excluded.content_hash,
               chunk_count   = excluded.chunk_count,
               chunk_size    = excluded.chunk_size,
               chunk_overlap = excluded.chunk_overlap,
               embed_model   = excluded.embed_model,
               updated_at    = excluded.updated_at""",
        (doc_id, content_hash, chunk_count, chunk_size,
         chunk_overlap, embed_model, datetime.now().isoformat()),
    )
    conn.commit()


def clear_index_states(conn):
    """清空全部向量化状态（集合被重建/更换嵌入模型后必须调用，否则会误判已同步）。"""
    conn.execute("DELETE FROM document_index_state")
    conn.commit()


def count_index_states(conn):
    return conn.execute(
        "SELECT COUNT(*) AS c FROM document_index_state"
    ).fetchone()["c"]


# document_chunks 表操作
def add_chunk(conn, document_id, chunk_index, content, embedding=None):
    """
    向 document_chunks 插入一个分块。
    embedding 为预留的 BLOB 字段（可传 bytes 或 None）。
    """
    conn.execute(
        """INSERT INTO document_chunks
           (document_id, chunk_index, content, embedding)
           VALUES (?, ?, ?, ?)""",
        (document_id, chunk_index, content, embedding),
    )
    conn.commit()


def add_chunks(conn, document_id, chunks):
    """
    批量写入某文档的全部分块（单事务提交，避免逐条 commit 的开销）。

    chunks 为内容字符串列表，chunk_index 从 0 递增。
    调用方需先 clear_chunks 清理旧分块（受 UNIQUE(document_id, chunk_index) 约束）。
    """
    if not chunks:
        return 0
    conn.executemany(
        """INSERT INTO document_chunks
           (document_id, chunk_index, content, embedding)
           VALUES (?, ?, ?, NULL)""",
        [(document_id, i, c) for i, c in enumerate(chunks)],
    )
    conn.commit()
    return len(chunks)


def clear_chunks(conn, document_id):
    """删除某文档的全部分块（重建分块前调用）。"""
    conn.execute(
        "DELETE FROM document_chunks WHERE document_id = ?",
        (document_id,),
    )
    conn.commit()


def get_chunks(conn, document_id):
    """返回某文档的全部分块（按 chunk_index 升序）。"""
    cur = conn.execute(
        """SELECT id, document_id, chunk_index, content, embedding
           FROM document_chunks
           WHERE document_id = ?
           ORDER BY chunk_index ASC""",
        (document_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def get_all_chunks(conn, limit=None):
    """分页/全量获取所有分块（供向量索引构建）。"""
    sql = """SELECT id, document_id, chunk_index, content, embedding
             FROM document_chunks ORDER BY document_id ASC, chunk_index ASC"""
    params = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


# 便捷统计
def count_by_source_type(conn):
    """返回 {source_type: 数量} 统计。"""
    rows = conn.execute(
        "SELECT source_type, COUNT(*) AS cnt FROM documents GROUP BY source_type"
    ).fetchall()
    return {r["source_type"]: r["cnt"] for r in rows}


# 量化/基本面数据 读写（第二阶段新增）
# 统一约定：
def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _norm_extra(row):
    extra = row.get("extra")
    if isinstance(extra, dict):
        row["extra"] = json.dumps(extra, ensure_ascii=False)
    return row


def upsert_daily(conn, rows):
    """批量写入日线行情（按 symbol+source+trade_date 冲突即更新）。返回影响行数。"""
    if not rows:
        return 0
    rows = [_norm_extra(dict(r)) for r in rows]
    ts = _now_iso()
    conn.executemany(
        """INSERT INTO stock_daily
           (symbol, source, trade_date, open, high, low, close,
            volume, amount, adj_close, extra, updated_at)
           VALUES (:symbol, :source, :trade_date, :open, :high, :low, :close,
                   :volume, :amount, :adj_close, :extra, :updated_at)
           ON CONFLICT(symbol, source, trade_date) DO UPDATE SET
             open=excluded.open, high=excluded.high, low=excluded.low,
             close=excluded.close, volume=excluded.volume, amount=excluded.amount,
             adj_close=excluded.adj_close, extra=excluded.extra,
             updated_at=excluded.updated_at""",
        [{"updated_at": ts, "open": None, "high": None, "low": None,
          "close": None, "volume": None, "amount": None, "adj_close": None,
          "extra": "{}", **r}
         for r in rows],
    )
    conn.commit()
    return len(rows)


def upsert_fundamental(conn, rows):
    """批量写入估值/市值类快照。"""
    if not rows:
        return 0
    rows = [_norm_extra(dict(r)) for r in rows]
    ts = _now_iso()
    conn.executemany(
        """INSERT INTO stock_fundamental
           (symbol, source, trade_date, pe_ttm, pe, pb, ps_ttm, market_cap,
            float_market_cap, dividend_yield, total_share, float_share,
            extra, updated_at)
           VALUES (:symbol, :source, :trade_date, :pe_ttm, :pe, :pb, :ps_ttm,
                   :market_cap, :float_market_cap, :dividend_yield,
                   :total_share, :float_share, :extra, :updated_at)
           ON CONFLICT(symbol, source, trade_date) DO UPDATE SET
             pe_ttm=excluded.pe_ttm, pe=excluded.pe, pb=excluded.pb,
             ps_ttm=excluded.ps_ttm, market_cap=excluded.market_cap,
             float_market_cap=excluded.float_market_cap,
             dividend_yield=excluded.dividend_yield,
             total_share=excluded.total_share, float_share=excluded.float_share,
             extra=excluded.extra, updated_at=excluded.updated_at""",
        [{"updated_at": ts, "pe_ttm": None, "pe": None, "pb": None, "ps_ttm": None,
          "market_cap": None, "float_market_cap": None, "dividend_yield": None,
          "total_share": None, "float_share": None, "extra": "{}", **r}
         for r in rows],
    )
    conn.commit()
    return len(rows)


def upsert_financial(conn, rows):
    """批量写入定期财务指标（按 symbol+source+report_date 冲突即更新）。"""
    if not rows:
        return 0
    rows = [_norm_extra(dict(r)) for r in rows]
    ts = _now_iso()
    conn.executemany(
        """INSERT INTO stock_financial
           (symbol, source, report_date, revenue, net_profit, roe, roa,
            gross_margin, net_margin, eps, bps, debt_ratio,
            operating_cashflow, extra, updated_at)
           VALUES (:symbol, :source, :report_date, :revenue, :net_profit, :roe,
                   :roa, :gross_margin, :net_margin, :eps, :bps, :debt_ratio,
                   :operating_cashflow, :extra, :updated_at)
           ON CONFLICT(symbol, source, report_date) DO UPDATE SET
             revenue=excluded.revenue, net_profit=excluded.net_profit,
             roe=excluded.roe, roa=excluded.roa,
             gross_margin=excluded.gross_margin, net_margin=excluded.net_margin,
             eps=excluded.eps, bps=excluded.bps, debt_ratio=excluded.debt_ratio,
             operating_cashflow=excluded.operating_cashflow,
             extra=excluded.extra, updated_at=excluded.updated_at""",
        [{"updated_at": ts, "revenue": None, "net_profit": None, "roe": None,
          "roa": None, "gross_margin": None, "net_margin": None, "eps": None,
          "bps": None, "debt_ratio": None, "operating_cashflow": None,
          "extra": "{}", **r}
         for r in rows],
    )
    conn.commit()
    return len(rows)


def log_sync(conn, source, task, symbol="", status="ok", rows_affected=0, message=""):
    """记录一次同步任务结果。"""
    now = _now_iso()
    conn.execute(
        """INSERT INTO data_sync_log
           (source, task, symbol, status, rows_affected, message, started_at, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, task, symbol, status, rows_affected, message[:500], now, now),
    )
    conn.commit()


def upsert_snapshot_rows(conn, rows):
    """批量写入快照行（trade_date+slot+kind+code 冲突即更新）。返回行数。"""
    if not rows:
        return 0
    rows = [_norm_extra(dict(r)) for r in rows]
    ts = _now_iso()
    conn.executemany(
        """INSERT INTO market_snapshot
           (trade_date, slot, kind, code, name, open, high, low, close,
            pct_chg, volume, amount, extra, updated_at)
           VALUES (:trade_date, :slot, :kind, :code, :name, :open, :high,
                   :low, :close, :pct_chg, :volume, :amount, :extra, :updated_at)
           ON CONFLICT(trade_date, slot, kind, code) DO UPDATE SET
             name=excluded.name, open=excluded.open, high=excluded.high,
             low=excluded.low, close=excluded.close, pct_chg=excluded.pct_chg,
             volume=excluded.volume, amount=excluded.amount,
             extra=excluded.extra, updated_at=excluded.updated_at""",
        [{"updated_at": ts, "open": None, "high": None, "low": None,
          "close": None, "pct_chg": None, "volume": None, "amount": None,
          "extra": "{}", **r}
         for r in rows],
    )
    conn.commit()
    return len(rows)


def upsert_stock_snapshot_rows(conn, rows):
    """批量写入个股快照（trade_date+slot+code 冲突即更新）。返回行数。"""
    if not rows:
        return 0
    ts = _now_iso()
    conn.executemany(
        """INSERT INTO stock_snapshot
           (trade_date, slot, code, symbol, name, close, pct_chg, open, high,
            low, prev_close, volume, amount, turnover, total_cap, float_cap,
            pe, pb, source, updated_at)
           VALUES (:trade_date, :slot, :code, :symbol, :name, :close, :pct_chg,
                   :open, :high, :low, :prev_close, :volume, :amount, :turnover,
                   :total_cap, :float_cap, :pe, :pb, :source, :updated_at)
           ON CONFLICT(trade_date, slot, code) DO UPDATE SET
             symbol=excluded.symbol, name=excluded.name, close=excluded.close,
             pct_chg=excluded.pct_chg, open=excluded.open, high=excluded.high,
             low=excluded.low, prev_close=excluded.prev_close,
             volume=excluded.volume, amount=excluded.amount,
             turnover=excluded.turnover, total_cap=excluded.total_cap,
             float_cap=excluded.float_cap, pe=excluded.pe, pb=excluded.pb,
             source=excluded.source, updated_at=excluded.updated_at""",
        [{"slot": "15:00", "source": "em", "updated_at": ts,
          "close": None, "pct_chg": None, "open": None, "high": None,
          "low": None, "prev_close": None, "volume": None, "amount": None,
          "turnover": None, "total_cap": None, "float_cap": None,
          "pe": None, "pb": None, "stock_name": None, **r}
         for r in rows],
    )
    conn.commit()
    return len(rows)


def upsert_board_members(conn, rows):
    """批量写入板块成分（board_kind+board_code+symbol 冲突即更新）。返回行数。"""
    if not rows:
        return 0
    ts = _now_iso()
    conn.executemany(
        """INSERT INTO board_members
           (board_kind, board_code, board_name, symbol, stock_name, source,
            updated_at)
           VALUES (:board_kind, :board_code, :board_name, :symbol,
                   :stock_name, :source, :updated_at)
           ON CONFLICT(board_kind, board_code, symbol) DO UPDATE SET
             board_name=excluded.board_name, stock_name=excluded.stock_name,
             source=excluded.source, updated_at=excluded.updated_at""",
        [{"stock_name": "", "source": "em", "updated_at": ts, **r}
         for r in rows],
    )
    conn.commit()
    return len(rows)


def upsert_ticker_universe(conn, rows):
    """刷新 A 股名称→代码字典（symbol 冲突即更新）。返回行数。"""
    if not rows:
        return 0
    rows = [dict(r) for r in rows]
    ts = _now_iso()
    conn.executemany(
        """INSERT INTO ticker_universe (symbol, name, exchange, market, updated_at)
           VALUES (:symbol, :name, :exchange, :market, :updated_at)
           ON CONFLICT(symbol) DO UPDATE SET
             name=excluded.name, exchange=excluded.exchange,
             market=excluded.market, updated_at=excluded.updated_at""",
        [{"updated_at": ts, "exchange": "", "market": "cn", **r}
         for r in rows],
    )
    conn.commit()
    return len(rows)


def add_news_tickers(conn, links):
    """写入 新闻→标的 关联（news_id+symbol 已存在则忽略）。返回新增条数。"""
    if not links:
        return 0
    before = conn.total_changes
    ts = _now_iso()
    conn.executemany(
        """INSERT OR IGNORE INTO news_tickers
           (news_id, symbol, name, method, matched_in, confidence, created_at)
           VALUES (:news_id, :symbol, :name, :method, :matched_in,
                   :confidence, :created_at)""",
        [{"created_at": ts, "name": "", "method": "rules",
          "matched_in": "title", "confidence": 1.0, **dict(l)}
         for l in links],
    )
    conn.commit()
    return conn.total_changes - before


def upsert_signal(conn, row):
    """写入/更新一条信号回测记录（news_id+symbol+horizon_days 冲突即更新）。"""
    if not row:
        return 0
    row = dict(row)
    ts = _now_iso()
    conn.execute(
        """INSERT INTO signal_outcomes
           (news_id, symbol, horizon_days, direction, direction_method,
            signal_date, signal_time, entry_date, entry_price,
            exit_date, exit_price, symbol_pct, bench_symbol, bench_pct,
            excess_pct, status, note, updated_at)
           VALUES (:news_id, :symbol, :horizon_days, :direction,
                   :direction_method, :signal_date, :signal_time,
                   :entry_date, :entry_price, :exit_date, :exit_price,
                   :symbol_pct, :bench_symbol, :bench_pct, :excess_pct,
                   :status, :note, :updated_at)
           ON CONFLICT(news_id, symbol, horizon_days) DO UPDATE SET
             direction=excluded.direction,
             direction_method=excluded.direction_method,
             signal_date=excluded.signal_date,
             signal_time=excluded.signal_time,
             entry_date=excluded.entry_date, entry_price=excluded.entry_price,
             exit_date=excluded.exit_date, exit_price=excluded.exit_price,
             symbol_pct=excluded.symbol_pct, bench_pct=excluded.bench_pct,
             excess_pct=excluded.excess_pct, status=excluded.status,
             note=excluded.note, updated_at=excluded.updated_at""",
        {"updated_at": ts, "direction": "neutral",
         "direction_method": "lexicon", "signal_time": "",
         "entry_date": None, "entry_price": None, "exit_date": None,
         "exit_price": None, "symbol_pct": None, "bench_symbol": "000300.SH",
         "bench_pct": None, "excess_pct": None, "status": "pending",
         "note": "", **row},
    )
    conn.commit()
    return 1


_SIGNAL_KEYS = ("news_id", "symbol", "horizon_days", "direction",
                "direction_method", "signal_date", "signal_time",
                "entry_date", "entry_price", "exit_date", "exit_price",
                "symbol_pct", "bench_symbol", "bench_pct", "excess_pct",
                "status", "note", "updated_at")


def _signal_param(row, ts):
    return {"updated_at": ts, "direction": "neutral",
            "direction_method": "lexicon", "signal_time": "",
            "entry_date": None, "entry_price": None, "exit_date": None,
            "exit_price": None, "symbol_pct": None,
            "bench_symbol": "000300.SH", "bench_pct": None,
            "excess_pct": None, "status": "pending", "note": "",
            **{k: v for k, v in dict(row).items() if k in _SIGNAL_KEYS}}


def add_signals_batch(conn, rows):
    """批量展开建信号：INSERT OR IGNORE，已有 news_id+symbol+horizon 跳过。

    单事务 executemany 替代逐行 upsert（每日几百条场景下快 ~1 个数量级）。
    返回实际新增条数。
    """
    rows = [dict(r) for r in rows if r.get("news_id") and r.get("symbol")
            and r.get("horizon_days") is not None]
    if not rows:
        return 0
    before = conn.total_changes
    ts = _now_iso()
    conn.executemany(
        """INSERT OR IGNORE INTO signal_outcomes
           (news_id, symbol, horizon_days, direction, direction_method,
            signal_date, signal_time, bench_symbol, updated_at)
           VALUES (:news_id, :symbol, :horizon_days, :direction,
                   :direction_method, :signal_date, :signal_time,
                   :bench_symbol, :updated_at)""",
        [_signal_param(r, ts) for r in rows],
    )
    conn.commit()
    return conn.total_changes - before


def upsert_signal_rows(conn, rows):
    """批量 upsert 信号结算结果（同 upsert_signal 语义，单事务）。"""
    rows = [dict(r) for r in rows if r.get("news_id") and r.get("symbol")]
    if not rows:
        return 0
    ts = _now_iso()
    conn.executemany(
        """INSERT INTO signal_outcomes
           (news_id, symbol, horizon_days, direction, direction_method,
            signal_date, signal_time, entry_date, entry_price,
            exit_date, exit_price, symbol_pct, bench_symbol, bench_pct,
            excess_pct, status, note, updated_at)
           VALUES (:news_id, :symbol, :horizon_days, :direction,
                   :direction_method, :signal_date, :signal_time,
                   :entry_date, :entry_price, :exit_date, :exit_price,
                   :symbol_pct, :bench_symbol, :bench_pct, :excess_pct,
                   :status, :note, :updated_at)
           ON CONFLICT(news_id, symbol, horizon_days) DO UPDATE SET
             direction=excluded.direction,
             direction_method=excluded.direction_method,
             signal_date=excluded.signal_date,
             signal_time=excluded.signal_time,
             entry_date=excluded.entry_date, entry_price=excluded.entry_price,
             exit_date=excluded.exit_date, exit_price=excluded.exit_price,
             symbol_pct=excluded.symbol_pct, bench_pct=excluded.bench_pct,
             excess_pct=excluded.excess_pct, status=excluded.status,
             note=excluded.note, updated_at=excluded.updated_at""",
        [_signal_param(r, ts) for r in rows],
    )
    conn.commit()
    return len(rows)


def snapshot_done(conn, trade_date, slot):
    """指定日期/时段快照是否已落库（任一 kind 有行即视为已跑）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM market_snapshot WHERE trade_date=? AND slot=?",
        (trade_date, slot),
    ).fetchone()
    return bool(row and row["n"])


def unlinked_news(conn, publish_date=None, limit=1000):
    """取还没有任何 ticker 关联的 news 文档（按发布日期可选过滤）。"""
    sql = ("SELECT id, title, content, publish_date, metadata FROM documents "
           "WHERE source_type='news' AND id NOT IN "
           "(SELECT news_id FROM news_tickers)")
    params = []
    if publish_date:
        sql += " AND publish_date = ?"
        params.append(publish_date)
    sql += f" ORDER BY publish_date ASC, id ASC LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def news_ticker_rows(conn, publish_date=None, limit=5000):
    """新闻+关联标的 明细行（用于建信号/看板）。"""
    sql = ("SELECT d.id AS news_id, d.title, d.publish_date, d.metadata, "
           "       t.symbol, t.name AS ticker_name, t.method, t.matched_in "
           "FROM news_tickers t JOIN documents d ON d.id = t.news_id "
           "WHERE d.source_type='news'")
    params = []
    if publish_date:
        sql += " AND d.publish_date = ?"
        params.append(publish_date)
    sql += f" ORDER BY d.publish_date ASC, d.id ASC LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def pending_signals(conn, limit=500, statuses=("pending", "nodata")):
    """按信号日升序取尚未算完的记录（nodata 会在数据补全后重试）。"""
    marks = ",".join("?" * len(statuses))
    sql = ("SELECT * FROM signal_outcomes WHERE status IN (" + marks +
           ") ORDER BY signal_date ASC, id ASC")
    return [dict(r) for r in conn.execute(sql, tuple(statuses)).fetchmany(int(limit))]


def query_daily(conn, symbol, start=None, end=None, source=None, limit=2000):
    """按时间范围查询日线（升序）；条件动态拼接。"""
    sql = ["SELECT * FROM stock_daily WHERE 1=1"]
    params = []
    if symbol:
        sql.append("AND symbol = ?")
        params.append(symbol)
    if source:
        sql.append("AND source = ?")
        params.append(source)
    if start:
        sql.append("AND trade_date >= ?")
        params.append(start)
    if end:
        sql.append("AND trade_date <= ?")
        params.append(end)
    sql.append(f"ORDER BY trade_date ASC LIMIT {int(limit)}")
    return [dict(r) for r in conn.execute(" ".join(sql), params).fetchall()]


def latest_fundamental(conn, symbol, source=None):
    sql = "SELECT * FROM stock_fundamental WHERE symbol = ?"
    params = [symbol]
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY trade_date DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def latest_financials(conn, symbol, source=None, periods=4):
    sql = "SELECT * FROM stock_financial WHERE symbol = ?"
    params = [symbol]
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY report_date DESC LIMIT ?"
    params.append(int(periods))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


init_db()
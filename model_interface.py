"""model_interface.py — 大模型调用接口（DeepSeek）"""

import os
import json
from datetime import datetime

import requests

def _secret(name, default=""):
    """环境变量优先；缺失时回退读取 ~/.deepseek_key 配置文件（KEY=VALUE 逐行）。

    这样无论从终端、网页 UI、cron 还是非交互 shell 启动，只要文件存在即可生效，
    不依赖 shell 是否 source 了 ~/.bashrc。
    """
    val = os.environ.get(name)
    if val:
        return val
    for path in (
        os.path.expanduser("~/.deepseek_key"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ):
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    if k.strip() == name:
                        v = v.strip().strip('"').strip("'")
                        if v:
                            return v
        except OSError:
            continue
    return default


API_KEY = _secret("DEEPSEEK_API_KEY", "")
BASE_URL = _secret("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
MODEL = _secret("DEEPSEEK_MODEL", "deepseek-chat")

DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 120        # HTTP 超时（秒）；调用方可用 timeout= 覆盖


def _normalize_math(text):
    """将模型返回内容里的公式写法统一为 Markdown 渲染友好的形式。

    - \\( ... \\)  → $ ... $（行内）
    - \\[ ... \\]  → $$ ... $$（独立成行）
    这样无论模型怎么包裹公式，在支持数学渲染的 Markdown 查看器
    （VS Code 预览 / Typora / Obsidian / GitHub）里都能正常显示；
    终端里输出的是标准 LaTeX 文本，不再有难看的转义括号。
    """
    if not text:
        return text
    text = text.replace(r"\[", "$$").replace(r"\]", "$$")
    text = text.replace(r"\(", "$").replace(r"\)", "$")
    return text


_MATH_STYLE_HINT = (
    "数学公式请使用 Markdown 数学写法：行内公式用单个美元符 $ 包裹、"
    "独立成行的公式用两个美元符 $$ 包裹；"
    "不要用反斜杠加圆括号/方括号包裹公式，也不要让正文出现孤立的裸 LaTeX 命令，"
    "保证公式在 Markdown 预览里能直接渲染。"
)


# 底层调用
_MODEL_CACHE: dict = {"model": ""}


def _resolve_model():
    """确定实际使用的模型名。

    优先级：环境变量 DEEPSEEK_MODEL（或 ~/.deepseek_key 同名列）> 探测缓存 >
    代码默认 MODEL。DeepSeek 不同部署/时段可用模型名不同（如 deepseek-chat /
    deepseek-v4-pro），直接写死会在模型下线后 400；这里探测一次 /models
    并按候选顺序挑第一个可用，结果进程内缓存。
    """
    env_model = os.environ.get("DEEPSEEK_MODEL") or _secret("DEEPSEEK_MODEL")
    if env_model:
        return env_model
    if _MODEL_CACHE["model"]:
        return _MODEL_CACHE["model"]
    if API_KEY:
        try:
            resp = requests.get(
                f"{BASE_URL.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {API_KEY}"}, timeout=10)
            resp.raise_for_status()
            avail = {m.get("id") for m in resp.json().get("data", [])}
            for cand in ("deepseek-chat", "deepseek-v4-pro", "deepseek-v4-flash"):
                if cand in avail:
                    _MODEL_CACHE["model"] = cand
                    return cand
        except Exception:
            pass  # 探测失败时回退到代码默认 MODEL，由调用方报错
    return MODEL


def chat_completion(system_prompt, user_message, temperature=None, max_tokens=None,
                    stream=False, on_delta=None, timeout=None):
    """调用 DeepSeek chat completions 接口，返回 assistant 文本。

    stream=True 时通过 SSE 逐段接收：每收到一段内容即调用
    on_delta(text_chunk)（可用于交互命令的逐字打印），返回仍为归一化全文。
    默认 stream=False 保持原有批处理语义（predict.py 等批量调用方不受影响）。
    timeout 为 None 时用 DEFAULT_TIMEOUT（120s），保持既有行为不变；
    predict.py 按 cfg['model']['timeout_seconds'] 传入，使该配置真正生效。
    """
    if not API_KEY:
        raise RuntimeError(
            "未设置 DEEPSEEK_API_KEY。请通过环境变量配置：export DEEPSEEK_API_KEY=\"API KEY\"（替换为真实密钥）"
        )

    url = f"{BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": _resolve_model(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature if temperature is not None else DEFAULT_TEMPERATURE,
        "max_tokens": max_tokens if max_tokens else DEFAULT_MAX_TOKENS,
        "stream": bool(stream),
    }
    tmo = float(timeout or DEFAULT_TIMEOUT)
    if not stream:
        resp = requests.post(url, headers=headers, json=payload, timeout=tmo)
        resp.raise_for_status()
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
            return _normalize_math(content)
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"DeepSeek 返回格式异常: {data}") from e

    resp = requests.post(url, headers=headers, json=payload,
                         timeout=tmo, stream=True)
    resp.raise_for_status()
    buf = []
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
            delta = (obj["choices"][0].get("delta") or {}).get("content")
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if delta:
            buf.append(delta)
            if on_delta is not None:
                on_delta(delta)
    text = "".join(buf)
    if not text:
        raise RuntimeError(f"DeepSeek 流式返回为空（原始响应被截断？）: 无 content")
    return _normalize_math(text)


# 上下文拼装辅助
def _format_context(docs, max_docs=8, max_chars=6000):
    """将检索结果格式化为模型上下文文本。"""
    if not docs:
        return ""
    lines = []
    for i, d in enumerate(docs[:max_docs]):
        title = d.get("title") or "(无标题)"
        stype = d.get("source_type") or ""
        date = d.get("publish_date") or ""
        snippet = (d.get("snippet") or (d.get("content") or "")[:600]).strip()
        lines.append(
            f"[{i + 1}] 类型={stype} | 标题={title} | 日期={date}\n"
            f"内容片段: {snippet}"
        )
    text = "\n\n".join(lines)
    return text[:max_chars]


def _context_to_sources(docs):
    """检索结果 → 简化来源列表（生成报告时标注）。"""
    out = []
    seen = set()
    for d in docs:
        rid = d.get("document_id") or d.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        out.append({
            "id": rid,
            "title": d.get("title", ""),
            "source_type": d.get("source_type", ""),
            "url": d.get("url", ""),
            "publish_date": d.get("publish_date", ""),
        })
    return out


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


# 1. answer_question
def answer_question(question, context=None, stream=False, on_delta=None):
    """
    调用 DeepSeek 回答问题。
    context 为可选的检索上下文（字符串或文档列表）。
    stream/on_delta 透传给 chat_completion（流式输出用）。
    """
    sys_prompt = (
        "你是一名专业的金融研究助理。优先基于提供的知识库上下文回答；"
        "只有当下文确实完全无法支撑提问时，才可结合通用金融知识作答，"
        "并在回答末尾用一句话简要说明「本部分为知识库未覆盖的通用知识补充」，"
        "不要在开头重复或渲染免责声明。回答使用简体中文，尽量结构化（分点/表格）；"
        "引用知识库的内容在相应段落标注来源编号即可，不要编造数据。"
        + _MATH_STYLE_HINT
    )

    if isinstance(context, (list, tuple)):
        ctx_text = _format_context(context)
    else:
        ctx_text = context or ""

    parts = []
    if ctx_text.strip():
        parts.append("【知识库检索结果】\n" + ctx_text)
    parts.append("【用户问题】\n" + question)
    user_msg = "\n\n".join(parts)
    return chat_completion(sys_prompt, user_msg, stream=stream, on_delta=on_delta)


# 2. analyze_trend
def analyze_trend(query, time_range=None, docs=None, stream=False, on_delta=None):
    """
    结合新闻/研报等数据生成趋势分析。
    time_range: 时间范围描述，如 "最近一周"、"2026-09-01 至 2026-09-07"
    docs: 可选的检索文档列表，未传入时自动从知识库检索。
    stream/on_delta 透传给 chat_completion（流式输出用）。
    """
    if docs is None:
        from query_knowledge import hybrid_search
        docs = hybrid_search(query, top_k=12)
        print(f"📊 已自动检索知识库 {len(docs)} 条相关资料")

    ctx = _format_context(docs, max_docs=12, max_chars=8000)
    period = time_range or "过去一段时间"

    sys_prompt = (
        "你是一名宏观经济与金融趋势分析师。请基于提供的知识资料，"
        "围绕用户主题做趋势分析：包含核心趋势判断、关键数据/事件时间线、"
        "驱动因素、风险与展望。只基于资料推断，避免无关臆测，用简体中文结构化输出。"
        + _MATH_STYLE_HINT
    )
    user_msg = (
        f"【分析时段】{period}\n"
        f"【分析主题】{query}\n\n"
        f"【知识库资料】\n{ctx if ctx.strip() else '(无可用资料，请基于一般金融知识作答并说明数据缺失)'}"
    )
    return chat_completion(sys_prompt, user_msg, stream=stream, on_delta=on_delta)


# 3. generate_report
def generate_report(topic, sources=None, docs=None, stream=False, on_delta=None):
    """
    基于指定来源/检索结果生成研究报告。
    sources: 指定来源 id 列表；docs: 外部传入文档列表（含标题/内容/元数据）。
    stream/on_delta 透传给 chat_completion（流式输出用）。
    """
    if docs is None:
        from query_knowledge import search_by_keyword, search_by_vector
        kw_docs = search_by_keyword(topic)
        vec_docs = search_by_vector(topic, top_k=15)
        # 合并去重
        merged, seen = [], set()
        for d in kw_docs + vec_docs:
            rid = d.get("id") or d.get("document_id")
            if rid in seen:
                continue
            seen.add(rid)
            merged.append(d)
        docs = merged

    if sources:
        source_ids = set(sources)
        docs = [d for d in docs if (d.get("id") or d.get("document_id")) in source_ids]

    ctx = _format_context(docs, max_docs=15, max_chars=10000)
    source_list = _context_to_sources(docs)

    sys_prompt = (
        "你是一名资深卖方研究员，擅长撰写规范的研究报告。请基于提供的资料，"
        "输出结构完整的报告：一、摘要与投资要点；二、行业/主题背景；三、核心分析；"
        "四、风险提示；五、参考资料。报告须客观、数据可溯源，使用简体中文与 Markdown。"
        + _MATH_STYLE_HINT
    )
    user_msg = (
        f"【报告主题】{topic}\n"
        f"【生成日期】{_today_str()}\n\n"
        f"【参考资料】\n{ctx if ctx.strip() else '(无可用资料，请基于一般知识撰写并注明信息来源受限)'}\n\n"
        f"【可引用来源清单】\n{json.dumps(source_list, ensure_ascii=False, indent=2)}"
    )
    return chat_completion(sys_prompt, user_msg, stream=stream, on_delta=on_delta)
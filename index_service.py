"""index_service.py — 常驻向量索引/嵌入服务（优化点 4「模型预热」的正确形态）"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEFAULT_PORT = int(os.environ.get("KB_INDEX_SERVICE_PORT", "8931"))


class _JsonSafe:
    """float32/float64 等不可直接 json 序列化的值转成原生 float。"""

    @staticmethod
    def default(o):
        if isinstance(o, (float, int)):
            return float(o)
        return str(o)


class _IndexApp:
    """进程内单例：模型 + 集合只加载一次，所有请求共享。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.ready = False
        self.error = ""
        self.embedding_fn = None
        self.collection = None

    def ensure_ready(self):
        with self._lock:
            if self.ready:
                return True
            import data_ingestion as di
            self.embedding_fn = di._default_embedding_fn()
            self.collection = di._get_collection()
            if self.collection is None:
                self.error = "Chroma 集合不可用（缺失依赖或 EF 加载失败）"
                return False
            self.ready = True
            print(f" 索引服务就绪: {di.KB_EMBEDDING_MODEL}，"
                  f"集合 knowledge_docs 现存 {self.collection.count()} 向量")
            return True

    def health(self):
        return {
            "status": "ok" if self.ready else "starting",
            "vectors": self.collection.count() if self.ready else -1,
            "model": os.environ.get(
                "KB_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
            "error": self.error or "",
        }

    def search(self, query, top_k=10, source_type=None):
        with self._lock:
            kwargs = {"query_texts": [query], "n_results": int(top_k)}
            if source_type:
                kwargs["where"] = {"source_type": source_type}
            res = self.collection.query(**kwargs)
            return json.loads(json.dumps(res, default=_JsonSafe.default))

    def embed(self, texts):
        if not isinstance(texts, list) or not texts:
            raise ValueError("texts 必须是非空数组")
        with self._lock:
            vecs = self.embedding_fn(texts)
            return json.loads(json.dumps(vecs, default=_JsonSafe.default))

    def sync(self, rebuild=False):
        import data_ingestion as di
        with self._lock:
            t0 = time.time()
            collection = di.get_or_create_vector_index(
                rebuild=bool(rebuild), collection=self.collection)
            self.collection = collection
            return {"rebuild": bool(rebuild), "vectors": collection.count(),
                    "elapsed_s": round(time.time() - t0, 2)}


_APP = _IndexApp()


class _Handler(BaseHTTPRequestHandler):
    server_version = "KBIndexService/1.0"

    def _send(self, code, obj, cors=True):
        body = json.dumps(obj, ensure_ascii=False,
                          default=_JsonSafe.default).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args):  # 静默 access log，保留手动错误输出
        return

    def _route(self, need_body=False):
        path = self.path.split("?")[0].rstrip("/")
        method = self.command
        if path == "/health":
            self._send(200, _APP.health())
            return
        if path == "" or path == "/":
            self._send(200, {
                "service": "financial-kb index_service",
                "status": _APP.health(),
                "usage": {
                    "GET  /health": "存活与集合状态",
                    "POST /search": '向量检索 {"query": "...", "top_k": 10, "source_type": "news"}',
                    "POST /sync":   '增量对账 {"rebuild": false}',
                    "POST /embed":  '批量编码 {"texts": ["..."]}',
                },
                "note": "检索请使用 POST /search，CLI 侧(query_knowledge)已自动接入本服务",
            })
            return
        if not _APP.ensure_ready():
            self._send(503, {"error": _APP.error or "服务尚未就绪"})
            return
        body = self._read_body() if need_body else {}
        try:
            if path == "/search":
                query = (body.get("query") or "").strip()
                if not query:
                    self._send(400, {"error": "query 不能为空"})
                    return
                top_k = int(body.get("top_k") or 10)
                source_type = (body.get("source_type") or "").strip() or None
                self._send(200, _APP.search(query, top_k=top_k,
                                            source_type=source_type))
            elif path == "/embed":
                self._send(200, _APP.embed(body.get("texts") or []))
            elif path == "/sync":
                self._send(200, _APP.sync(rebuild=bool(body.get("rebuild"))))
            else:
                print(f" [index_service] 未知端点: {method} {self.path}")
                self._send(404, {
                    "error": f"未知端点: {method} {self.path}",
                    "valid_endpoints": ["/health", "/search", "/sync", "/embed"],
                })
        except Exception as e:  # noqa: BLE001
            print(f" [index_service] {path} 处理失败: {e}")
            self._send(500, {"error": str(e)})

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route(need_body=True)

    def do_OPTIONS(self):
        # 浏览器跨域预检：允许任意来源访问
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="常驻向量索引/嵌入服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--sync", action="store_true",
                        help="启动前先做一次增量对账（含首次 bootstrap/孤儿清理）")
    args = parser.parse_args()

    print(" 正在启动索引服务（首次冷加载模型约 11s，仅此一次）...")
    if args.sync:
        print(" 启动前增量对账中 ...")
        _APP.sync(rebuild=False)
    if not _APP.ensure_ready():
        print(f" 索引服务启动失败: {_APP.error}")
        sys.exit(1)

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f" 索引服务运行中: http://{args.host}:{args.port} "
          f"(KB_INDEX_SERVICE 探测地址)  Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n 索引服务已停止")


if __name__ == "__main__":
    main()

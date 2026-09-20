"""main.py — 金融知识库爬虫/上传/检索/问答 统一入口"""

import os
import sys
import argparse

# 以下重模块统一定位到本文件同目录
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _ensure_deps():
    """运行前保证基础依赖可导入（含第三方库缺失时的清晰提示）。"""
    missing = []
    for mod in ("requests", "bs4", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(" 缺少依赖: " + ", ".join(missing))
        print("   请安装: pip install requests beautifulsoup4 pyyaml")
        sys.exit(1)


# 命令实现
def cmd_crawl(args):
    """
    抓取新闻并入库。

    - 默认（未传 --vectorize）：先逐条抓取入库，抓取完成后由本函数
      统一调用 get_or_create_vector_index() 构建索引；
    - 传 --vectorize：爬虫在每条新闻入库的瞬间立即向量化。

    无论向量化结果如何，已抓取入库的数据不受影响（失败仅打印提示）。
    """
    _ensure_deps()
    import crawler

    vectorize = bool(getattr(args, "vectorize", False))

    if vectorize:
        # --vectorize：流式逐条立即向量化
        print("\n 开始抓取（--vectorize 逐条向量化模式）...")
        print("   → 每条新闻入库后立即构建向量索引，适合增量小批量抓取，无需等待末尾统一构建")
        crawler.crawler_main(vectorize=True)
        print("\n 抓取结束：已按 --vectorize 逐条向量化，无需再统一构建索引")
        return

    print("\n 开始抓取并入库（默认模式，enabled=true 的站点）...")
    print("   → 步骤 1/2：逐条抓取新闻并写入 SQLite 知识库（暂不逐条向量化）")
    crawler.crawler_main(vectorize=False)

    print("\n 步骤 2/2：正在为本次新增内容统一构建 Chroma 向量索引（增量 upsert）...")
    import data_ingestion
    try:
        data_ingestion.get_or_create_vector_index(rebuild=False)
        print(" 向量索引构建完成")
    except Exception as e:
        print(f" 向量索引构建失败（不影响已抓取入库的数据）: {e}")
    print("\n 抓取全部完成")


def cmd_stats(_args):
    import query_knowledge
    stats = query_knowledge.get_statistics()
    print(f"\n 知识库统计")
    print(f"   数据库文件: {stats['db_path']}")
    print(f"   文档总数:   {stats['total_documents']}")
    print("   按来源:")
    for typ, cnt in stats["by_source_type_ordered"].items():
        print(f"     - {typ:10s}: {cnt}")
    print(f"   文本分块数: {stats['total_chunks']}")
    print("   raw 归档文件:")
    for typ, cnt in stats["raw_file_counts"].items():
        print(f"     - {typ:10s}: {cnt}")


def cmd_search(args):
    import query_knowledge
    kw = args.keyword
    stype = args.type
    try:
        results = query_knowledge.search_by_keyword(kw, source_type=stype)
    except ValueError as e:
        print(f" {e}")
        return 1

    if not results:
        print(f" 未找到与 “{kw}” 相关的文档。")
        return 0

    print(f"\n “{kw}” 关键词搜索结果"
          + (f"（类型={stype}）" if stype else "") + f"，共 {len(results)} 条:"
          )
    for i, r in enumerate(results[:args.top], start=1):
        score = r.get("score") or 0
        print(f"\n[{i}] {r['title']}（{r['source_type']}）")
        print(f"    发布时间: {r.get('publish_date') or '未知'}")
        if r.get("authors"):
            print(f"    作者: {r['authors']}")
        if r.get("url"):
            print(f"    来源: {r['url']}")
        print(f"    相关度: {score}")
        print(f"    片段: {r.get('snippet') or '(无正文可用)'[:200]}")


def cmd_ask(args):
    """问答。默认先向量/关键词混合检索 top N 作为上下文，再调模型。"""
    import query_knowledge

    q = args.question
    top = args.top

    print(f" 正在检索知识库...")
    try:
        docs = query_knowledge.hybrid_search(q, top_k=top)
    except Exception as e:
        print(f" 检索失败（继续无上下文回答）: {e}")
        docs = []

    if docs:
        print(f"  已检索到 {len(docs)} 条相关资料作为上下文")
        for i, d in enumerate(docs, start=1):
            print(f"    [{i}] {d['title']}（{d['source_type']}）")
    else:
        print("  知识库无相关内容，将仅基于模型知识回答")

    import model_interface

    print(" 正在生成回答（流式输出）...")
    try:
        def _emit(chunk):
            sys.stdout.write(chunk)
            sys.stdout.flush()
        answer = model_interface.answer_question(q, context=docs,
                                                 stream=True, on_delta=_emit)
    except RuntimeError as e:
        print(f"\n 模型调用失败: {e}")
        print("   提示: 设置 export DEEPSEEK_API_KEY=\"API KEY\"（替换为真实密钥）后重试；"
              "或使用 `search/trend/report` 指令离线使用知识库。")
        return 1
    print("\n" + "=" * 60)

    if docs:
        print("\n 参考来源:")
        for i, d in enumerate(docs, start=1):
            print(f"   [{i}] {d['title']}（{d['source_type']}）"
                  + (f" | {d['url']}" if d.get("url") else ""))


def cmd_trend(args):
    import query_knowledge
    import model_interface

    q = args.topic
    try:
        docs = query_knowledge.hybrid_search(q, top_k=args.top)
    except Exception as e:
        print(f" 检索失败: {e}")
        docs = []
    print(f" 执行趋势分析：{q}（时段: {args.range or '未指定'}）")
    print(" 正在生成分析（流式输出）...")
    try:
        def _emit(chunk):
            sys.stdout.write(chunk)
            sys.stdout.flush()
        result = model_interface.analyze_trend(q, time_range=args.range,
                                               docs=docs, stream=True,
                                               on_delta=_emit)
    except RuntimeError as e:
        print(f"\n 模型调用失败: {e}")
        return 1
    print("\n" + "=" * 60)


def cmd_report(args):
    import query_knowledge
    import model_interface

    topic = args.topic

    # 收集 docs 以生成引用来源
    docs = []
    if not args.no_retrieve:
        try:
            docs = query_knowledge.hybrid_search(topic, top_k=args.top)
            print(f" 检索到 {len(docs)} 条相关来源")
        except Exception as e:
            print(f" 检索失败，报告将不引用知识库: {e}")
    print(" 正在生成报告（流式输出）...")
    try:
        def _emit(chunk):
            sys.stdout.write(chunk)
            sys.stdout.flush()
        content = model_interface.generate_report(topic, docs=docs,
                                                  stream=True, on_delta=_emit)
    except RuntimeError as e:
        print(f"\n 模型调用失败: {e}")
        return 1

    print("\n" + "=" * 60)

    if args.save:
        out = args.save
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(content + "\n")
        print(f" 报告已保存: {out}")


def cmd_rebuild(_args):
    import data_ingestion
    print(" 清空并重建 Chroma 向量索引 ...")
    try:
        data_ingestion.get_or_create_vector_index(rebuild=True)
        print(" 向量索引重建完成")
    except Exception as e:
        print(f" 向量索引重建失败: {e}")


def cmd_upload(rest_args):
    """转发 upload.py 全部剩余参数（upload 之后的原样透传）。"""
    import database
    database.init_db()
    import upload
    return upload.main(rest_args)


# argparse
def build_parser():
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="金融知识库统一入口：爬取/上传/检索/问答/趋势/报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python main.py crawl\n"
               "  python main.py crawl --vectorize\n"
               "  python main.py search 央行 货币政策 --top 5\n"
               "  python main.py ask 央行近期货币政策有哪些动向\n"
               "  python main.py trend 新能源板块 --range 最近一周\n"
               "  python main.py report 2026年半导体行业展望 --save report.md\n"
               "  python main.py upload paper paper.pdf --meta meta.json\n",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # crawl
    p_crawl = sub.add_parser("crawl", help="抓取启用网站并入库")
    p_crawl.add_argument("--vectorize", action="store_true",
                         help="抓取过程中逐条立即向量化"
                              "（默认：先入库，抓取完成后统一构建向量索引）")

    # stats / status
    sub.add_parser("stats", help="查看知识库统计")
    sub.add_parser("status", help="等价 stats")

    # search
    p_search = sub.add_parser("search", help="关键词搜索")
    p_search.add_argument("keyword", help="搜索关键词")
    p_search.add_argument("--top", type=int, default=10)
    p_search.add_argument("--type", default=None,
                          choices=["news", "paper", "textbook", "report"],
                          help="按来源类型过滤")

    # ask
    p_ask = sub.add_parser("ask", help="大模型问答（检索增强）")
    p_ask.add_argument("question", help="问题")
    p_ask.add_argument("--top", type=int, default=5, help="检索上下文条数")
    p_ask.add_argument("--model-key", default="", help=argparse.SUPPRESS)

    # trend
    p_trend = sub.add_parser("trend", help="趋势分析")
    p_trend.add_argument("topic", help="分析主题")
    p_trend.add_argument("--top", type=int, default=10)
    p_trend.add_argument("--range", default=None,
                         help="时间范围，如 最近一周 / 2026-09-01 至 2026-09-07")

    # report
    p_report = sub.add_parser("report", help="生成研究报告")
    p_report.add_argument("topic", help="报告主题")
    p_report.add_argument("--top", type=int, default=10)
    p_report.add_argument("--save", default=None, help="保存 Markdown 文件路径")
    p_report.add_argument("--no-retrieve", action="store_true",
                          help="不检索知识库直接生成")

    # rebuild
    sub.add_parser("rebuild", help="重建向量索引")

    # upload 转发（吃掉紧随的参数）
    p_upload = sub.add_parser("upload", help="上传 PDF（论文/教科书/研报）")
    p_upload.add_argument("rest", nargs=argparse.REMAINDER,
                          help="透传给 upload.py 的参数，如 paper file.pdf --meta m.json")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command or args.command in ("stats", "status"):
        if not args.command:
            parser.print_help()
            return 0
        try:
            cmd_stats(args)
        except Exception as e:
            print(f" 统计失败: {e}")
            return 1
        return 0

    if args.command == "upload":
        return cmd_upload(args.rest or [])

    dispatch = {
        "crawl": cmd_crawl,
        "search": cmd_search,
        "ask": cmd_ask,
        "trend": cmd_trend,
        "report": cmd_report,
        "rebuild": cmd_rebuild,
    }
    fn = dispatch.get(args.command)
    if not fn:
        parser.print_help()
        return 0

    # crawl 内部会自行初始化数据库，其余命令先确保表结构可用
    if args.command != "crawl":
        import database
        database.init_db()

    try:
        return fn(args)
    except KeyboardInterrupt:
        print("\n 已中断")
        return 130
    except Exception as e:
        print(f" 执行失败: {e}")
        if os.environ.get("KB_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
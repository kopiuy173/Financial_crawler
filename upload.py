"""upload.py — 手动上传入库工具"""

import os
import sys
import json
import argparse

import database
import data_ingestion

TYPE_ALIASES = {
    "paper": "paper",
    "papers": "paper",
    "pdf": "paper",
    "论文": "paper",
    "学术": "paper",
    "textbook": "textbook",
    "textbooks": "textbook",
    "教科书": "textbook",
    "教材": "textbook",
    "report": "report",
    "reports": "report",
    "研报": "report",
    "报告": "report",
}


def load_metadata(meta_path):
    """读取可选元数据 JSON 文件。"""
    if not meta_path:
        return {}
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    if not isinstance(meta, dict):
        raise ValueError("元数据文件必须包含 JSON 对象")
    return meta


def format_meta_json_from_args(args):
    """通过 --title/--authors 等简化命令行元数据录入。"""
    meta = {}
    if args.title:
        meta["title"] = args.title
    if args.authors:
        meta["authors"] = args.authors
    if args.abstract:
        meta["abstract"] = args.abstract
    if args.publish_date:
        meta["publish_date"] = args.publish_date
    if args.tags:
        meta["tags"] = args.tags
    if args.url:
        meta["url"] = args.url
    return meta


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="将论文/教科书/研报 PDF 上传入库到统一知识库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "doc_type",
        help="文档类型: paper / textbook / report",
    )
    parser.add_argument(
        "file_path",
        help="PDF 文件路径",
    )
    parser.add_argument(
        "--meta", dest="meta_path",
        help="元数据 JSON 文件路径（可选字段见文件头注释）",
    )
    parser.add_argument("--title", help="标题（可选，默认用文件名）")
    parser.add_argument("--authors", help="作者（可选）")
    parser.add_argument("--abstract", help="摘要（可选）")
    parser.add_argument("--publish-date", dest="publish_date", help="发布日期 YYYY-MM-DD")
    parser.add_argument("--tags", help="逗号分隔标签")
    parser.add_argument("--url", help="来源链接")
    parser.add_argument(
        "--no-vector", dest="vectorize", action="store_false", default=True,
        help="跳过 Chroma 向量化（默认会尝试）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅校验参数与文件、不真正入库",
    )
    args = parser.parse_args(argv)

    # 1. 类型归一
    src_type = TYPE_ALIASES.get(args.doc_type.strip().lower())
    if src_type is None:
        print(f"❌ 未知文档类型: {args.doc_type}")
        print("   可用类型: paper / textbook / report（支持中英文别名）")
        return 2

    # 2. 文件检查
    if not os.path.exists(args.file_path):
        print(f"❌ 文件不存在: {args.file_path}")
        return 2
    if not args.file_path.lower().endswith(".pdf"):
        print(f"⚠️  文件不是 PDF 后缀: {args.file_path}")

    # 3. 组装元数据
    meta = {}
    if args.meta_path:
        if not os.path.exists(args.meta_path):
            print(f"❌ 元数据文件不存在: {args.meta_path}")
            return 2
        meta.update(load_metadata(args.meta_path))
    cli_meta = format_meta_json_from_args(args)
    # CLI 参数优先级高于 JSON 文件
    meta.update(cli_meta)

    if args.dry_run:
        print("🧪 Dry-run 校验通过：")
        print(f"   类型: {src_type}")
        print(f"   文件: {args.file_path}")
        print(f"   标题: {meta.get('title') or os.path.basename(args.file_path)}")
        print(f"   元数据: {json.dumps(meta, ensure_ascii=False, indent=2)}")
        print("   （未写入任何数据）")
        return 0

    # 4. 入库
    ingest_fn = {
        "paper": data_ingestion.ingest_paper,
        "textbook": data_ingestion.ingest_textbook,
        "report": data_ingestion.ingest_report,
    }[src_type]

    try:
        inserted, doc_id, raw_path = ingest_fn(
            args.file_path, metadata=meta, vectorize=args.vectorize
        )
        print(f"✅ 上传完成: type={src_type}")
        print(f"   document_id: {doc_id}")
        print(f"   原始文件归档: {raw_path}")
        print(f"   状态: {'新入库' if inserted else '已存在（跳过重复写入）'}")
        return 0
    except Exception as e:
        print(f"❌ 上传失败: {e}")
        return 1


if __name__ == "__main__":
    # 确保数据库可用后才进入正式流程
    try:
        database.init_db()
    except Exception as e:
        print(f"❌ 数据库初始化失败: {e}")
        sys.exit(1)
    sys.exit(main())
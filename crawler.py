print("爬虫脚本已启动")
import os
import time
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import yaml

import database  # storage_resolve / 默认库路径（D 盘优先）
import data_ingestion

# 加载配置
def load_config():
    with open("config/sites.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def get_enabled_sites(config):
    return [site for site in config.get("sites", []) if site.get("enabled", False)]


CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
SCHEDULE_PATH = os.path.join(CONFIG_DIR, "schedule.yaml")
FALLBACK_WINDOW = {"id": "evening", "at": "17:30", "cutoff": "17:30"}

NEWS_SAVE_ROOT = database.storage_resolve("NEWS_SAVE_ROOT", ("新闻",), "data")
NEWS_HTML_DIR = database.storage_resolve(
    "NEWS_HTML_DIR", ("新闻", "raw_html"), os.path.join("data", "raw_html"))
PARSED_JSON_DIR = database.storage_resolve(
    "NEWS_PARSED_DIR", ("新闻", "parsed"), os.path.join("data", "parsed"))


def load_schedule():
    """读取 config/schedule.yaml；缺失时回退为旧式单窗口（17:30，无配额）。"""
    try:
        with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError:
        data = {}
    windows = data.get("windows") or [dict(FALLBACK_WINDOW)]
    return {
        "timezone": data.get("timezone", "Asia/Shanghai"),
        "daily_quota_bytes": int(data.get("daily_quota_bytes") or 2097152),
        "window_poll_seconds": int(data.get("window_poll_seconds") or 60),
        "windows": windows,
    }


def schedule_window(window_id):
    sched = load_schedule()
    for w in sched["windows"]:
        if w.get("id") == window_id:
            return w
    raise KeyError(f"schedule.yaml 中不存在窗口: {window_id}")


def window_cutoff_dt(window_id, base=None):
    """某窗口当天的新闻截止时间（date 取 base 当天，默认今天）。"""
    w = schedule_window(window_id)
    hh, mm = [int(x) for x in str(w.get("cutoff") or "17:30").split(":")[:2]]
    base = base or datetime.now()
    return base.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _news_item_bytes(raw_news):
    """按入库口径估算单条 UTF-8 字节：title + body/text + 固定开销。"""
    title = str(raw_news.get("title") or "").encode("utf-8")
    body = str(raw_news.get("body") or raw_news.get("text") or "").encode("utf-8")
    return len(title) + len(body) + 128


def fetch_page(url, global_config, proxy_config):
    headers = {"User-Agent": global_config["global"]["user_agent"]}
    proxies = None
    if proxy_config.get("enabled"):
        proxies = {"http": proxy_config["http"], "https": proxy_config["https"]}
    try:
        resp = requests.get(url, headers=headers, proxies=proxies, timeout=global_config["global"]["timeout"])
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f" 抓取失败: {url} - {e}")
        return None

def save_raw_html(html, site_name, url):
    os.makedirs(NEWS_HTML_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{site_name}_{timestamp}.html"
    path = os.path.join(NEWS_HTML_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path

def parse_article(html, selectors):
    from bs4 import BeautifulSoup
    import json
    import re

    soup = BeautifulSoup(html, "html.parser")
    
    # 1. 定位包含数据的 <script> 标签
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag:
        print(" 未找到 __NEXT_DATA__ 脚本。")
        return {}, soup

    try:
        # 2. 提取并解析 JSON 数据
        json_text = script_tag.string
        if not json_text:
            print(" 脚本标签内没有内容。")
            return {}, soup
        
        data = json.loads(json_text)
        
        # 3. 根据你提供的 HTML 结构，精确提取新闻列表
        page_info = data.get("props", {}).get("pageProps", {}).get("initSsrData", {}).get("pageInfo", {})
        if not page_info:
            print(" 未找到 pageInfo 数据。")
            return {}, soup
            
        # 根据你的HTML，list 是一个数组，我们取第一个元素
        first_list = page_info.get("list", [])
        if not first_list or not isinstance(first_list, list) or len(first_list) == 0:
            print(" list 为空或格式不正确。")
            return {}, soup
            
        # timeList 存放着所有新闻
        time_list = first_list[0].get("timeList", [])
        if not time_list:
            print(" timeList 为空。")
            return {}, soup

        # 4. 处理列表中的第一条新闻作为示例
        first_news = time_list[0]
        parsed_data = {
            "title": first_news.get("title", ""),
            "date": first_news.get("time", ""),
            "body": first_news.get("text", ""),
            "contId": first_news.get("contId", ""),
            "firstPublishTime": first_news.get("firstPublishTime", "")
        }
        print(f" 成功解析 JSON 数据: {parsed_data['title']}")
        return parsed_data, soup

    except json.JSONDecodeError as e:
        print(f" JSON 解析失败: {e}")
        # 打印出错位置附近的内容以便调试
        print(f"      问题 JSON 开头: {json_text[:200]}...")
        return {}, soup
    except Exception as e:
        print(f" 提取数据时发生未知错误: {e}")
        return {}, soup

def save_parsed_content(site_name, parsed_data):
    os.makedirs(PARSED_JSON_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{site_name}_{timestamp}.json"
    path = os.path.join(PARSED_JSON_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(parsed_data, f, ensure_ascii=False, indent=2)
    return path

def crawl_site(site_config, global_config, proxy_config, vectorize=True,
               cutoff_dt=None, byte_budget=None, run_limit=None):
    name = site_config["name"]
    url = site_config["url"]
    selectors = site_config.get("selectors", {})
    max_pages = site_config.get("max_pages", 1)
    # 默认 50，可被 sites.yaml 的 max_articles 覆盖（有配额时以配额为准）
    save_images = site_config.get("save_images", False)
    max_articles = int(site_config.get("max_articles", 50))
    if run_limit is not None:
        max_articles = max(max_articles, int(run_limit))

    today = datetime.now()
    if cutoff_dt is None:
        cutoff_dt = today.replace(hour=17, minute=30, second=0, microsecond=0)

    os.makedirs("logs", exist_ok=True)
    seen_file_path = os.path.join("logs", "seen_ids.txt")
    seen_ids = set()
    if os.path.exists(seen_file_path):
        with open(seen_file_path, "r", encoding="utf-8") as f:
            for line in f:
                cid = line.strip()
                if cid:
                    seen_ids.add(cid)

    out_dir = os.path.join(NEWS_SAVE_ROOT, today.strftime("%Y-%m-%d"))
    os.makedirs(out_dir, exist_ok=True)

    print(f"\n 开始抓取: {name}")
    print(f"   URL: {url}")
    print(f" 已记录的 contId 数量: {len(seen_ids)}")
    print(f" 时间过滤: 只抓取今天 {cutoff_dt.strftime('%H:%M')} 之前（含）发布的新闻（按从新到旧）")
    print(f" 数量限制: 本次最多抓取 {max_articles} 条"
          + (f"（本窗口配额 {byte_budget} 字节，先到先停）"
             if byte_budget is not None else "（手动/兼容模式）"))
    print(f" 输出目录: {out_dir}")

    def _parse_page_items(html_text):
        soup_local = BeautifulSoup(html_text, "html.parser")
        script_tag = soup_local.find("script", id="__NEXT_DATA__")
        if not script_tag or not script_tag.string:
            return [], soup_local
        try:
            data = json.loads(script_tag.string)
            page_info = (data.get("props", {}).get("pageProps", {})
                             .get("initSsrData", {}).get("pageInfo", {}))
            first_list = page_info.get("list", [])
            if not first_list or not isinstance(first_list, list) or not first_list:
                return [], soup_local
            time_list = first_list[0].get("timeList", [])
            return list(time_list) if time_list else [], soup_local
        except Exception as e:
            print(f" 页面新闻列表解析失败: {e}")
            return [], soup_local

    def _extract_pub_time(raw_news):
        ts_text = str(raw_news.get("firstPublishTime", "")).strip()
        if ts_text:
            try:
                ts = float(ts_text)
                if ts > 1e12:  # 毫秒时间戳 → 秒
                    ts /= 1000.0
                return datetime.fromtimestamp(ts)
            except (ValueError, OSError, OverflowError):
                pass
        time_text = str(raw_news.get("time", "")).strip()
        import re
        m = re.search(r"(\d{1,2}):(\d{2})", time_text)
        if m:
            try:
                return today.replace(hour=int(m.group(1)),
                                     minute=int(m.group(2)),
                                     second=0, microsecond=0)
            except ValueError:
                return None
        return None

    # 时间戳降序 → 从新到旧
    def _pub_ts_key(raw_news):
        try:
            return float(str(raw_news.get("firstPublishTime", "")).strip())
        except (ValueError, TypeError):
            return 0.0

    saved_count = 0
    spent_bytes = 0
    stop_all = False

    for page_num in range(1, max_pages + 1):
        if stop_all or saved_count >= max_articles:
            break

        page_url = url if page_num == 1 else f"{url}?page={page_num}"
        print(f" 第 {page_num} 页: {page_url}")

        html = fetch_page(page_url, global_config, proxy_config)
        if not html:
            continue

        raw_path = save_raw_html(html, name, page_url)
        print(f" HTML 已保存: {raw_path}")

        page_items, soup = _parse_page_items(html)
        if not page_items:
            print(" 本页无新闻，继续下一页")
            time.sleep(global_config["global"]["download_delay"])
            continue

        page_items.sort(key=_pub_ts_key, reverse=True)  # 从新到旧

        for raw_news in page_items:
            # ---- 3. 数量上限判断 ----
            if saved_count >= max_articles:
                print(f" 已达本次上限 {max_articles} 条，停止翻页")
                stop_all = True
                break

            pub_time = _extract_pub_time(raw_news)
            if pub_time is None:
                print(" 无法解析发布时间，跳过该条")
                continue
            if pub_time.date() != today.date():
                print(f" 新闻发布于 {pub_time.strftime('%Y-%m-%d %H:%M')}"
                      f"（早于今天），已按从新到旧扫描，后续只会更旧，停止翻页")
                stop_all = True
                break
            if pub_time > cutoff_dt:
                print(f" 发布时间 {pub_time.strftime('%H:%M')}"
                      f" 晚于今天 {cutoff_dt.strftime('%H:%M')}，跳过该条，继续查找较早新闻")
                continue

            cont_id = str(raw_news.get("contId", "")).strip()
            if cont_id and cont_id in seen_ids:
                print(f" 命中已抓取 contId={cont_id}，停止翻页")
                stop_all = True
                break

            # ---- 组装并保存本条新闻 ----
            page_images = []
            if save_images:
                for img in soup.find_all("img"):
                    src = img.get("src")
                    if src:
                        page_images.append(urljoin(page_url, src))

            record = {
                "title": raw_news.get("title", ""),
                "date": raw_news.get("time", ""),
                "body": raw_news.get("text", ""),
                "contId": raw_news.get("contId", ""),
                "firstPublishTime": raw_news.get("firstPublishTime", ""),
                "source_url": page_url,
                "site_name": name,
                "crawled_at": datetime.now().isoformat(),
            }
            if save_images:
                record["images"] = page_images

            est_bytes = _news_item_bytes(raw_news) if byte_budget is not None else 0
            if byte_budget is not None and spent_bytes + est_bytes > byte_budget:
                print(f" 达到本窗口字节配额（{byte_budget} B），停止翻页")
                stop_all = True
                break

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            parsed_path = os.path.join(out_dir, f"{name}_{timestamp}.json")
            seq = 1
            while os.path.exists(parsed_path):
                parsed_path = os.path.join(out_dir, f"{name}_{timestamp}_{seq}.json")
                seq += 1

            with open(parsed_path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)

            try:
                data_ingestion.ingest_news(record, vectorize=vectorize)
            except Exception as e:
                print(f" 统一入库失败: {e}")

            saved_count += 1
            spent_bytes += est_bytes
            print(f" [{saved_count}/{max_articles}] 已保存: {parsed_path}")
            print(f" 发布时间: {pub_time.strftime('%Y-%m-%d %H:%M')}")

            if cont_id:
                seen_ids.add(cont_id)
                with open(seen_file_path, "a", encoding="utf-8") as f:
                    f.write(cont_id + "\n")

        if stop_all:
            break

        time.sleep(global_config["global"]["download_delay"])

    print(f" {name} 抓取完成，本次共新增 {saved_count} 条新闻（目录: {out_dir}）"
          + (f"，入库字节约 {spent_bytes} B" if byte_budget is not None else ""))
    return saved_count, spent_bytes


def _http_get_json(url, global_config, proxy_config):
    """GET 一个 JSON 接口并解析；兼容东财 JSONP（var ajaxResult={...};）。失败返回 None。"""
    headers = {"User-Agent": global_config["global"]["user_agent"]}
    proxies = None
    if proxy_config.get("enabled"):
        proxies = {"http": proxy_config["http"], "https": proxy_config["https"]}
    try:
        resp = requests.get(url, headers=headers, proxies=proxies,
                            timeout=global_config["global"]["timeout"])
        resp.raise_for_status()
        text = resp.text.strip()
        if text.startswith("var ") or "ajaxResult" in text[:40]:
            start, end = text.find("{"), text.rfind("}")
            text = text[start:end + 1] if start != -1 and end > start else "{}"
        return json.loads(text)
    except Exception as e:
        print(f" JSON 接口抓取失败: {url} - {e}")
        return None


def _epoch_to_hhmm(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


def _wscn_items(payload):
    """华尔街见闻 live JSON → 统一 raw_news 列表。"""
    items = []
    for it in ((payload.get("data") or {}).get("items") or []):
        if not isinstance(it, dict):
            continue
        ts = it.get("display_time")
        cid = it.get("id")
        title = str(it.get("title") or "").strip()
        text = str(it.get("content_text") or "").strip()
        if cid is None or not ts or not text:
            continue
        items.append({
            "title": title, "text": text,
            "contId": f"wscn_{cid}",
            "firstPublishTime": int(ts),
            "time": _epoch_to_hhmm(ts),
            "source_url": str(it.get("uri") or ""),
            "site_name": "wallstreetcn",
        })
    return items


def _em_items(payload):
    """东方财富 7×24（JSONP LivesList）→ 统一 raw_news 列表。"""
    items = []
    for it in (payload.get("LivesList") or []):
        if not isinstance(it, dict):
            continue
        cid = it.get("newsid") or it.get("id")
        title = str(it.get("title") or "").strip()
        text = str(it.get("digest") or it.get("simdigest")
                   or it.get("summary") or "").strip()
        if cid is None or not text:
            continue
        ts = None
        for key in ("showtime", "ordertime"):
            raw = str(it.get(key) or "").strip()
            if raw:
                try:
                    ts = int(datetime.strptime(raw[:19],
                                               "%Y-%m-%d %H:%M:%S").timestamp())
                    break
                except ValueError:
                    continue
        if ts is None:
            continue
        items.append({
            "title": title, "text": text,
            "contId": f"em_{cid}",
            "firstPublishTime": ts,
            "time": _epoch_to_hhmm(ts),
            "source_url": str(it.get("url_w") or it.get("url_unique") or ""),
            "site_name": "eastmoney",
        })
    return items


def _sina_items(payload):
    """新浪财经滚动 JSON → 统一 raw_news 列表。"""
    items = []
    data = (payload.get("result") or {}).get("data") or []
    for it in data:
        if not isinstance(it, dict):
            continue
        ts = it.get("ctime") or it.get("intime")
        cid = it.get("docid")
        title = str(it.get("title") or "").strip()
        text = str(it.get("intro") or it.get("wapsummary")
                   or it.get("summary") or "").strip()
        if not cid or not ts or not text:
            continue
        items.append({
            "title": title, "text": text,
            "contId": f"sina_{cid}",
            "firstPublishTime": int(ts),
            "time": _epoch_to_hhmm(ts),
            "source_url": str(it.get("url") or it.get("wapurl") or ""),
            "site_name": "sina",
            "tags": str(it.get("media_name") or ""),
        })
    return items


JSON_SOURCE_PARSERS = {
    "wallstreetcn": _wscn_items,
    "eastmoney": _em_items,
    "sina": _sina_items,
}

def crawl_site_json(site_config, global_config, proxy_config, vectorize=True,
                    cutoff_dt=None, byte_budget=None, run_limit=None):
    """抓取一个 JSON 快讯源：分页拉取 → 窗口 cutoff 截断 → 增量去重 → 配额控制 → 落盘 + 入库。"""
    name = site_config["name"]
    parser = JSON_SOURCE_PARSERS.get(site_config.get("parser", ""))
    if not parser:
        print(f" JSON parser 未注册: {name}，跳过")
        return 0, 0
    api_url_tpl = site_config["api_url"]
    max_pages = int(site_config.get("max_pages", 1))
    max_articles = int(site_config.get("max_articles", 50))
    if run_limit is not None:
        max_articles = max(max_articles, int(run_limit))
    # 配额模式翻页深度：max_pages_budget（未配默认 12 页）
    if byte_budget is not None:
        max_pages = max(max_pages,
                        int(site_config.get("max_pages_budget", 12)))
    if "{page}" not in api_url_tpl:
        max_pages = 1

    today = datetime.now()
    if cutoff_dt is None:
        cutoff_dt = today.replace(hour=17, minute=30, second=0, microsecond=0)

    os.makedirs("logs", exist_ok=True)
    seen_file_path = os.path.join("logs", "seen_ids.txt")
    seen_ids = set()
    if os.path.exists(seen_file_path):
        with open(seen_file_path, "r", encoding="utf-8") as f:
            for line in f:
                cid = line.strip()
                if cid:
                    seen_ids.add(cid)

    out_dir = os.path.join(NEWS_SAVE_ROOT, today.strftime("%Y-%m-%d"))
    os.makedirs(out_dir, exist_ok=True)

    print(f"\n 开始抓取(JSON): {name}")
    print(f"   API: {api_url_tpl}")
    print(f" 时间过滤: 只抓取今天 {cutoff_dt.strftime('%H:%M')} 之前（含）发布（按从新到旧）")
    print(f" 数量限制: 本次最多抓取 {max_articles} 条"
          + (f"（本窗口配额 {byte_budget} 字节，先到先停）"
             if byte_budget is not None else "（手动/兼容模式）"))

    saved_count = 0
    spent_bytes = 0
    stop_all = False
    unparseable = 0

    for page_num in range(1, max_pages + 1):
        if stop_all or saved_count >= max_articles:
            break
        page_url = api_url_tpl.replace("{page}", str(page_num))
        print(f" 第 {page_num} 页: {page_url}")

        payload = _http_get_json(page_url, global_config, proxy_config)
        if payload is None:
            continue
        page_items = parser(payload)
        if not page_items:
            print(" 本页无有效条目，尝试下一页")
            time.sleep(global_config["global"]["download_delay"])
            continue

        page_items.sort(key=lambda r: float(r.get("firstPublishTime") or 0),
                        reverse=True)

        for raw_news in page_items:
            if saved_count >= max_articles:
                print(f" 已达本次上限 {max_articles} 条，停止翻页")
                stop_all = True
                break

            ts_text = str(raw_news.get("firstPublishTime", "")).strip()
            try:
                pub_time = datetime.fromtimestamp(float(ts_text))
            except (ValueError, OSError, OverflowError):
                unparseable += 1
                continue
            if pub_time.date() != today.date():
                print(f" 新闻发布于 {pub_time.strftime('%Y-%m-%d %H:%M')}"
                      f"（早于今天），停止翻页")
                stop_all = True
                break
            if pub_time > cutoff_dt:
                continue

            cont_id = str(raw_news.get("contId", "")).strip()
            if cont_id and cont_id in seen_ids:
                print(f" 命中已抓取 contId={cont_id}，停止翻页")
                stop_all = True
                break

            record = {
                "title": raw_news.get("title", ""),
                "date": raw_news.get("time", ""),
                "body": raw_news.get("text", ""),
                "contId": cont_id,
                "firstPublishTime": raw_news.get("firstPublishTime", ""),
                "source_url": raw_news.get("source_url", ""),
                "site_name": name,
                "crawled_at": datetime.now().isoformat(),
            }
            if raw_news.get("tags"):
                record["tags"] = raw_news["tags"]

            est_bytes = _news_item_bytes(raw_news) if byte_budget is not None else 0
            if byte_budget is not None and spent_bytes + est_bytes > byte_budget:
                print(f" 达到本窗口字节配额（{byte_budget} B），停止翻页")
                stop_all = True
                break

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            parsed_path = os.path.join(out_dir, f"{name}_{timestamp}.json")
            seq = 1
            while os.path.exists(parsed_path):
                parsed_path = os.path.join(out_dir, f"{name}_{timestamp}_{seq}.json")
                seq += 1
            with open(parsed_path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)

            try:
                data_ingestion.ingest_news(record, vectorize=vectorize)
            except Exception as e:
                print(f" 统一入库失败: {e}")

            saved_count += 1
            spent_bytes += est_bytes
            print(f" [{saved_count}/{max_articles}] 已保存: {parsed_path}")

            if cont_id:
                seen_ids.add(cont_id)
                with open(seen_file_path, "a", encoding="utf-8") as f:
                    f.write(cont_id + "\n")

        time.sleep(global_config["global"]["download_delay"])

    print(f" {name} 抓取完成，本次新增 {saved_count} 条"
          + (f"（{unparseable} 条时间无法解析被跳过）" if unparseable else "")
          + (f"，入库字节约 {spent_bytes} B" if byte_budget is not None else ""))
    return saved_count, spent_bytes


def main(vectorize=True, window_id=None, source_budget_bytes=None, only=None):
    """
    抓取入口。

    vectorize=True  : 每条新闻入库后立即向量化（流式，由爬虫内部处理）
    vectorize=False : 只抓取入库、不逐条向量化，由调用方随后统一构建索引

    window_id : config/schedule.yaml 里的窗口 id（morning/noon/evening）；
                None = 手动/兼容模式（当天 17:30 截断，仅按 sites.yaml 上限）
    source_budget_bytes : 每个来源本窗口的字节配额；None 且指定 window_id 时
                按「全天额度 ÷ 窗口数 ÷ 来源数」自动均分
    only      : 只抓指定 site.name（单源调试用）
    """
    config = load_config()
    global_config = config
    proxy_config = config.get("proxy", {})
    sites = get_enabled_sites(config)
    if only:
        sites = [s for s in sites if s.get("name") == only]
        if not sites:
            raise SystemExit(f"未找到启用的站点: {only}")

    sched = load_schedule() if window_id else None
    cutoff_dt = window_cutoff_dt(window_id) if window_id else None
    budget = source_budget_bytes
    if budget is None and window_id is not None:
        n_windows = max(1, len(sched["windows"]))
        budget = int((sched["daily_quota_bytes"] or 2097152)
                     / (n_windows * max(1, len(sites))))
    run_limit = 8000 if budget is not None else None

    print(f" 共 {len(sites)} 个网站需要抓取"
          + ("（逐条向量化模式）" if vectorize else "（入库模式，索引稍后统一构建）"))
    if window_id is not None:
        print(f" 调度窗口: {window_id}，新闻截止 {cutoff_dt.strftime('%H:%M')}"
              f"，每来源字节配额 {budget} B")
    else:
        print(" 手动/兼容模式：当天 17:30 截断，按 sites.yaml 上限（无字节配额）")

    total_saved = 0
    total_bytes = 0
    for site in sites:
        try:
            kwargs = dict(vectorize=vectorize, cutoff_dt=cutoff_dt,
                          byte_budget=budget, run_limit=run_limit)
            if site.get("type") == "json":
                n, b = crawl_site_json(site, global_config, proxy_config, **kwargs)
            else:
                n, b = crawl_site(site, global_config, proxy_config, **kwargs)
            total_saved += n or 0
            total_bytes += b or 0
        except Exception as e:
            print(f" 抓取 {site['name']} 时发生异常: {e}")
        time.sleep(5)

    print("\n 所有任务执行完毕。")
    if window_id is not None:
        print(f" 窗口 {window_id} 汇总: 新增 {total_saved} 条 / {total_bytes} B")
    return {"window": window_id, "saved": total_saved, "bytes": total_bytes}


crawler_main = main


if __name__ == "__main__":
    import argparse

    sched = load_schedule()
    win_ids = [w["id"] for w in sched["windows"]]
    p = argparse.ArgumentParser(description="金融快讯爬虫（三时段窗口 + 字节配额）")
    p.add_argument("--window", choices=win_ids, default=None,
                   help="调度窗口 id（来自 config/schedule.yaml）；缺省=手动/兼容模式")
    p.add_argument("--budget-bytes", type=int, default=None,
                   help="每来源字节配额（指定 --window 且缺省时按 schedule.yaml 均分）")
    p.add_argument("--only", default=None, help="只抓指定站点名（单源调试）")
    p.add_argument("--no-vectorize", dest="vectorize", action="store_false",
                   help="入库后不逐条向量化（由调用方统一构建索引）")
    p.set_defaults(vectorize=True)
    args = p.parse_args()
    main(vectorize=args.vectorize, window_id=args.window,
         source_budget_bytes=args.budget_bytes, only=args.only)

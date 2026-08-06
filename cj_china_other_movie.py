import os
import sys
import re
import time
import json
import threading
from bs4 import BeautifulSoup

# 导入指纹库，高精度模拟浏览器
from curl_cffi import requests as curl_requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 🛠️ 路径配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_JSON = os.path.join(BASE_DIR, "queue.json")
RESULTS_DIR = os.path.join(BASE_DIR, "results")

# 配置单次 Actions 运行抓取的数量
BATCH_SIZE = 3000
MAX_WORKERS = 6

# 确保存放结果的文件夹存在
os.makedirs(RESULTS_DIR, exist_ok=True)


# ==================== 高精度详情页提取 ====================
def parse_movie(movie_url):
    """
    解析详情页，返回提取到的字典数据。若解析失败则返回 None。
    """
    LOCAL_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Referer": "https://gcbt.net/"
    }

    thread_scraper = curl_requests.Session()
    resp = None
    max_fetch_attempts = 3

    for attempt in range(1, max_fetch_attempts + 1):
        try:
            resp = thread_scraper.get(
                movie_url,
                headers=LOCAL_HEADERS,
                impersonate="chrome120",
                timeout=12
            )
            if resp.status_code == 200:
                break
            else:
                time.sleep(0.5)
        except Exception:
            time.sleep(0.5)

    if not resp or resp.status_code != 200:
        return None

    try:
        soup = BeautifulSoup(resp.text, 'html.parser')

        # 提取标题
        title = ""
        if soup.title:
            raw_title = soup.title.string.strip()
            title = raw_title.rsplit(' - ', 1)[0].strip()

        if not title:
            title_tag = soup.find('h1', class_=re.compile(r'entry-title|title'))
            title = title_tag.text.strip() if title_tag else ""

        if not title:
            return None

        # 提取发布时间
        release_date = ""
        time_tag = soup.find('time', datetime=True)
        if time_tag:
            release_date = time_tag['datetime'][:10]
        else:
            release_date = time.strftime("%Y-%m-%d")

        # 提取哈希与拼装磁力
        download_hash = ""
        magnet_link = ""

        for a in soup.find_all('a', href=True):
            href = a['href']
            match = re.search(r'hash=([a-f0-9]{40,43})', href, re.IGNORECASE)
            if match:
                download_hash = match.group(1)
                break

        if not download_hash:
            text_matches = re.findall(r'\b([a-f0-9]{40,43})\b', resp.text, re.IGNORECASE)
            if text_matches:
                download_hash = text_matches[0]

        if download_hash:
            bt_hash = download_hash[3:].lower() if len(download_hash) == 43 else download_hash.lower()
            code = bt_hash
            magnet_link = f"magnet:?xt=urn:btih:{bt_hash}&dn={re.sub(r'\s+', '%20', title)}"
        else:
            url_match = re.search(r'/(\d+)\.html', movie_url)
            code = f"GCBT-{url_match.group(1)}" if url_match else f"GCBT-{str(int(time.time()))}"

        # 提取图片
        entry_content = soup.find('div', class_='entry-content')
        images = []
        if entry_content:
            for img in entry_content.find_all('img'):
                src = ""
                for attr in ['data-original', 'data-src', 'data-lazy-src', 'src']:
                    val = img.get(attr)
                    if val and val.startswith(('http://', 'https://')):
                        src = val
                        break
                if src and src not in images:
                    images.append(src)

        cover_url = images[0] if images else ""
        preview_images = images

        # 类别标签
        genres = []
        tags_header = soup.find('h5', string=lambda text: text and "Tags" in text)
        if tags_header:
            tags_p = tags_header.find_next_sibling('p')
            if tags_p:
                raw_text = tags_p.get_text()
                genres = [t.strip() for t in re.split(r'\s+', raw_text) if t.strip()]

        # 组装磁力
        magnets = []
        if magnet_link:
            magnets.append({
                "title": title + " 磁力下载链接",
                "link": magnet_link,
                "size": "未知大小",
                "share_date": release_date,
                "hd": True
            })

        return {
            "code": code,
            "title": title,
            "cover_url": cover_url,
            "release_date": release_date,
            "genres": genres,
            "preview_images": preview_images,
            "magnets": magnets,
            "source_url": movie_url
        }

    except Exception:
        return None


# ==================== 主控逻辑 ====================
def main():
    print("ℹ️ GitHub Actions 批量采集主程序启动...")

    if not os.path.exists(QUEUE_JSON):
        print(f"❌ 仓库中未找到 {QUEUE_JSON}，请先在本地生成并推送。")
        return

    with open(QUEUE_JSON, "r", encoding="utf-8") as f:
        queue_data = json.load(f)

    # 过滤出 status 为 0 的链接，并限制单次处理的数量
    pending_urls = [url for url, status in queue_data.items() if status == 0][:BATCH_SIZE]
    total_pending = len(pending_urls)

    if total_pending == 0:
        print("🏁 所有数据已处理完毕，或未发现待爬取链接。")
        return

    print(f"📦 本次计划采集链接数量: {total_pending} 条")

    results = []
    results_lock = threading.Lock()

    def worker(url):
        data = parse_movie(url)
        if data:
            with results_lock:
                results.append(data)
            queue_data[url] = 1  # 状态改为成功 (1)
            print(f"  ✅ 成功: {url}")
        else:
            queue_data[url] = 2  # 状态改为失败 (2)
            print(f"  ❌ 失败: {url}")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker, url): url for url in pending_urls}
        completed = 0
        for _ in as_completed(futures):
            completed += 1
            if completed % 20 == 0 or completed == total_pending:
                print(f"📊 采集进度: {completed}/{total_pending}...")

    # 如果抓取到了有效数据，则将其保存为独立的临时 JSON 文件
    if results:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        batch_filename = os.path.join(RESULTS_DIR, f"batch_{timestamp}.json")
        with open(batch_filename, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"💾 成功生成本次结果数据: {batch_filename}")

    # 将更新了状态的整个 queue.json 写回磁盘，等待 Actions 自动 commit
    with open(QUEUE_JSON, "w", encoding="utf-8") as f:
        json.dump(queue_data, f, ensure_ascii=False, indent=2)
    print("💾 已更新 queue.json 中的链接状态")


if __name__ == "__main__":
    main()

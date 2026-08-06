import os
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
URLS_FILE = os.path.join(BASE_DIR, "urls.txt")
RESULTS_DIR = os.path.join(BASE_DIR, "results")

# 每次 GitHub Actions 运行只消费 10,000 条（耗时约 30-40 分钟，极度安全，绝不超时）
BATCH_LIMIT = 1000
MAX_WORKERS = 4

# 确保存放结果的文件夹存在
os.makedirs(RESULTS_DIR, exist_ok=True)


# ==================== 详情页高精度提取 ====================
def parse_movie(movie_url):
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

        # 组装磁力结构
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


# ==================== 主控任务分发 ====================
def main():
    print("ℹ️ GitHub Actions 定量消费爬虫启动...")

    if not os.path.exists(URLS_FILE):
        print(f"❌ 未找到待采集链接文件: {URLS_FILE}")
        return

    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

    total_urls = len(urls)
    if total_urls == 0:
        print("🏁 任务队列已空！数据全部采集完成。")
        return

    # 切片出本次要抓取的链接，和剩余未抓取的链接
    chunk = urls[:BATCH_LIMIT]
    remaining = urls[BATCH_LIMIT:]

    print(f"📦 队列总计剩余 {total_urls} 条。本次计划处理前 {len(chunk)} 条，留下 {len(remaining)} 条供下一次处理。")

    results = []
    failed_urls = []
    results_lock = threading.Lock()
    failed_lock = threading.Lock()

    def worker(url):
        data = parse_movie(url)
        if data:
            with results_lock:
                results.append(data)
            print(f"  ✅ 成功: {url}")
        else:
            with failed_lock:
                failed_urls.append(url)
            print(f"  ❌ 失败: {url}")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker, url): url for url in chunk}
        completed = 0
        for _ in as_completed(futures):
            completed += 1
            if completed % 100 == 0 or completed == len(chunk):
                print(f"📊 当前批次进度: {completed}/{len(chunk)}")

    # 1. 落地保存抓取成功的数据
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    if results:
        batch_file = os.path.join(RESULTS_DIR, f"batch_{timestamp}.json")
        with open(batch_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"💾 成功保存本次抓取结果共 {len(results)} 条 -> {batch_file}")

    # 2. 落地保存抓取失败的链接（失败也算处理过，不能继续留在待抓取队列里）
    if failed_urls:
        failed_file = os.path.join(RESULTS_DIR, f"failed_{timestamp}.txt")
        with open(failed_file, "w", encoding="utf-8") as f:
            for f_url in failed_urls:
                f.write(f_url + "\n")
        print(f"💾 成功记录本次失败链接共 {len(failed_urls)} 条 -> {failed_file}")

    # 3. 用剩余未爬取的链接重写覆盖 urls.txt，实现文件自消耗变短
    with open(URLS_FILE, "w", encoding="utf-8") as f:
        for r_url in remaining:
            f.write(r_url + "\n")
    print(f"💾 urls.txt 已更新，文件已缩减，剩余待处理链接: {len(remaining)} 条")


if __name__ == "__main__":
    main()

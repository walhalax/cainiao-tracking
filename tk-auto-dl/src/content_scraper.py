import httpx
from bs4 import BeautifulSoup
import logging
from datetime import datetime, timedelta
import re
from urllib.parse import urljoin, urldefrag, urlparse, urlunparse
import asyncio
from typing import Optional

# ロギング設定 (INFOレベルに戻す)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')

TARGET_URL = "https://tktube.com/ja/categories/fc2/"
REQUIRED_APPROVAL_RATE = 70
REQUIRED_DAYS_PASSED = 3 # 公開から最低3日経過

async def fetch_html(url: str) -> Optional[str]:
    """指定されたURLからHTMLコンテンツを取得する非同期関数"""
    logging.debug(f"Fetching HTML from: {url}")
    try:
        async with httpx.AsyncClient() as client:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            response = await client.get(url, headers=headers, follow_redirects=True, timeout=30.0)
            logging.debug(f"HTTP Status Code: {response.status_code} for {url}")
            if response.status_code == 404:
                logging.warning(f"ページが見つかりません (404): {url}")
                return None
            response.raise_for_status()
            return response.text
    except httpx.RequestError as e:
        logging.error(f"HTTPリクエストエラーが発生しました: {url} - {e}")
        return None
    except Exception as e:
        logging.error(f"HTML取得中に予期せぬエラーが発生しました: {url} - {e}", exc_info=True)
        return None

def parse_videos(html_content: str) -> list[dict]:
    """HTMLコンテンツを解析し、動画情報のリストを抽出する"""
    videos = []
    soup = BeautifulSoup(html_content, 'html.parser')
    video_items = soup.select('div.list-videos div.item')
    logging.debug(f"Found {len(video_items)} video items on the page.")

    for i, item in enumerate(video_items):
        try:
            video_info = {}
            link_tag = item.find('a')
            video_info['url'] = link_tag['href'] if link_tag and link_tag.has_attr('href') else None

            title_tag = item.find('strong', class_='title')
            video_info['title'] = title_tag.text.strip() if title_tag else None

            img_tag = item.find('img', class_='thumb')
            video_info['thumbnail'] = img_tag['src'] if img_tag and 'src' in img_tag.attrs else None
            video_info['preview'] = img_tag['data-preview'] if img_tag and 'data-preview' in img_tag.attrs else None

            added_tag = item.find('div', class_='added')
            video_info['added_date_str'] = None
            video_info['added_date'] = None
            if added_tag and added_tag.find('em'):
                 date_str = added_tag.find('em').text.strip()
                 video_info['added_date_str'] = date_str
                 try:
                     video_info['added_date'] = datetime.strptime(date_str, '%Y-%m-%d')
                 except ValueError:
                     logging.warning(f"日付形式の解析に失敗しました: {date_str} for {video_info.get('title')}")

            rating_tag = item.find('div', class_='rating')
            video_info['rating'] = None
            if rating_tag:
                rating_text = rating_tag.text.strip().replace('%', '')
                try:
                    video_info['rating'] = int(rating_text)
                except ValueError:
                    logging.warning(f"評価形式の解析に失敗しました: {rating_tag.text.strip()} for {video_info.get('title')}")

            duration_tag = item.find('div', class_='duration')
            video_info['duration'] = duration_tag.text.strip() if duration_tag else None

            video_info['fc2_id'] = None
            video_info['fc2_id_num'] = None
            if video_info['title'] and 'FC2-PPV-' in video_info['title']:
                 match = re.search(r'FC2-PPV-(\d+)', video_info['title'])
                 if match:
                     video_info['fc2_id'] = match.group(0)
                     video_info['fc2_id_num'] = match.group(1)

            if not all([video_info.get('url'), video_info.get('title'), video_info.get('fc2_id')]):
                 logging.warning(f"必須情報(URL, Title, FC2 ID)が欠落しているためスキップ: Item {i+1}")
                 continue

            videos.append(video_info)

        except Exception as e:
            logging.error(f"動画情報 Item {i+1} の解析中にエラーが発生しました: {e}", exc_info=True)
            continue

    return videos

def filter_videos(videos: list[dict], processed_ids: set[str], oldest_date: Optional[datetime] = None) -> list[dict]: # oldest_date パラメータ追加
    """動画リストを条件に基づいてフィルタリングする"""
    filtered = []
    today = datetime.now()
    # 公開日の閾値（今日を含まないN日前）
    threshold_date_passed = datetime(today.year, today.month, today.day) - timedelta(days=REQUIRED_DAYS_PASSED)
    logging.debug(f"Filtering videos. Processed IDs count: {len(processed_ids)}, Required days passed threshold: {threshold_date_passed.strftime('%Y-%m-%d')}, Oldest date threshold: {oldest_date.strftime('%Y-%m-%d') if oldest_date else 'None'}")

    for video in videos:
        fc2_id = video.get('fc2_id')
        title = video.get('title')
        added_date = video.get('added_date')
        rating = video.get('rating')
        added_date_str = video.get('added_date_str')

        # 0. タイトルに除外キーワードが含まれていないか
        exclude_keywords = ["妻", "熟", "美魔女", "30代", "40代", "50代"]
        if title and any(keyword in title for keyword in exclude_keywords):
            logging.debug(f"Skipping (excluded keyword in title): {title}")
            continue

        # 1. 過去に処理済みでないか
        if fc2_id in processed_ids:
            logging.debug(f"Skipping (processed): {fc2_id} - {title}")
            continue

        # 2. 公開日が取得できているか
        if not added_date:
            logging.debug(f"Skipping (no date): {title}")
            continue

        # 3. 指定日数経過しているか (threshold_date_passed より *前* であること)
        if added_date >= threshold_date_passed:
            logging.debug(f"Skipping (too recent): Date='{added_date_str}', Threshold='{threshold_date_passed.strftime('%Y-%m-%d')}' - {title}")
            continue

        # 4. 巡回期間内か (oldest_date より *後* であること) - oldest_date が指定されている場合のみ
        if oldest_date and added_date < oldest_date:
             logging.debug(f"Skipping (too old): Date='{added_date_str}', Oldest='{oldest_date.strftime('%Y-%m-%d')}' - {title}")
             continue # 期間外なのでスキップ

        # 5. 評価が取得できているか、かつ指定の支持率以上か
        if rating is None or rating < REQUIRED_APPROVAL_RATE:
            logging.debug(f"Skipping (rating): Rating={rating}% - {title}")
            continue

        # すべての条件を満たす場合
        filtered.append(video)
        logging.info(f"Adding to eligible: {fc2_id} - {title} (Date: {added_date_str}, Rating: {rating}%)")

    return filtered

async def scrape_eligible_videos(
    start_url: str,
    processed_ids: set[str],
    max_pages: int = 5,
    oldest_date: Optional[datetime] = None # oldest_date パラメータ追加
) -> list[dict]:
    """指定されたURLから開始し、条件に合う動画情報を複数ページにわたって収集する (ページ番号方式)"""
    eligible_videos = []
    base_url = start_url.rstrip('/') + '/'
    current_page = 1
    stop_scraping_due_to_date = False # 日付制限で停止したかのフラグ

    while current_page <= max_pages:
        url_to_fetch = base_url if current_page == 1 else f"{base_url}{current_page}/"
        logging.info(f"{current_page}ページ目をスクレイピング中: {url_to_fetch}")
        html = await fetch_html(url_to_fetch)

        if not html:
            logging.warning(f"HTMLの取得に失敗したか、ページが存在しませんでした ({url_to_fetch})。スクレイピングを終了します。")
            break
        else:
            videos_on_page = parse_videos(html)
            logging.info(f"{len(videos_on_page)} 件の動画情報をページから抽出しました。")

            if not videos_on_page and current_page > 1:
                 logging.info(f"{current_page}ページには動画がありませんでした。最終ページと判断し、スクレイピングを終了します。")
                 break

            # フィルタリング (期間制限を渡す)
            filtered_on_page = filter_videos(videos_on_page, processed_ids, oldest_date)
            logging.info(f"{len(filtered_on_page)} 件の動画がダウンロード条件を満たしました。")
            eligible_videos.extend(filtered_on_page)

            # 期間制限チェック: このページで見つかった動画が全て oldest_date より古い場合、
            # それ以降のページも古い可能性が高いので探索を打ち切る
            if oldest_date and videos_on_page: # ページに動画がある場合のみチェック
                 all_too_old = True
                 for v in videos_on_page:
                     if v.get('added_date') and v.get('added_date') >= oldest_date:
                         all_too_old = False
                         break
                 if all_too_old:
                     logging.info(f"{current_page}ページの動画は全て期間外 ({oldest_date.strftime('%Y-%m-%d')}より前) でした。スクレイピングを終了します。")
                     stop_scraping_due_to_date = True
                     break # ループを抜ける

            current_page += 1
            await asyncio.sleep(1)

    if not stop_scraping_due_to_date and current_page > max_pages:
        logging.warning(f"最大ページ数 ({max_pages}) に達したため、スクレイピングを終了します。")

    logging.info(f"合計 {len(eligible_videos)} 件のダウンロード対象動画が見つかりました。")
    return eligible_videos

# --- テスト用 ---
async def main_test():
    print("テストスクレイピングを開始します...")
    processed_ids_test = {"FC2-PPV-4668098", "FC2-PPV-4672939"}
    one_month_ago_test = datetime.now() - timedelta(days=30)
    print(f"期間制限: {one_month_ago_test.strftime('%Y-%m-%d')} 以降")
    eligible = await scrape_eligible_videos(TARGET_URL, processed_ids_test, max_pages=10, oldest_date=one_month_ago_test) # テスト用に最大10ページ
    print("\n--- ダウンロード対象 ---")
    if eligible:
        for v in eligible:
            print(f"- ID: {v.get('fc2_id')}, Title: {v.get('title')}, Added: {v.get('added_date_str')}, Rating: {v.get('rating')}%")
    else:
        print("ダウンロード対象の動画は見つかりませんでした。")
    print("----------------------")

if __name__ == '__main__':
    asyncio.run(main_test())
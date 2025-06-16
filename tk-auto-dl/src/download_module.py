import asyncio
from bs4 import BeautifulSoup
import logging
import os
import asyncio
from typing import Callable, Dict, Any, Optional, Coroutine # Coroutine を追加
from urllib.parse import urljoin, urldefrag # URL解析のために追加 (urlparse, urlunparse は未使用なので削除)
import re # JavaScript解析用にインポート
import httpx # httpx を再度インポート
import platform # OS判定用に追加
from pathlib import Path # クロスプラットフォームなパス操作用に追加

# ロギング設定
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# web_app側で設定するのでここではコメントアウト or 削除しても良い
# logging.getLogger(__name__).setLevel(logging.DEBUG) # 個別モジュールでレベル設定する場合

# --- ダウンロードディレクトリパス取得 ---
def get_download_directory() -> str:
    """
    OS標準のダウンロードディレクトリ内に 'tk-auto-dl' フォルダを作成し、そのパスを返す。
    フォルダが存在しない場合は作成する。
    """
    home_dir = str(Path.home())
    download_dir = os.path.join(home_dir, "Downloads", "tk-auto-dl")

    # ディレクトリが存在しない場合は作成
    if not os.path.exists(download_dir):
        try:
            os.makedirs(download_dir)
            logging.info(f"ダウンロードディレクトリを作成しました: {download_dir}")
        except OSError as e:
            logging.error(f"ダウンロードディレクトリの作成に失敗しました: {download_dir} - {e}")
            # 作成に失敗した場合は、カレントディレクトリに 'downloads' を使うなどのフォールバックも検討できるが、
            # 今回はエラーとして扱う
            raise e # ディレクトリ作成失敗は致命的なので例外を再発生させる

    logging.debug(f"ダウンロードディレクトリパス: {download_dir}")
    return download_dir


# --- MP4 URL 抽出 ---
async def find_mp4_url(video_page_url: str) -> Optional[str]:
    """
    動画ページのURLからMP4ファイルの直接URLを見つける。
    tktube.com の構造に合わせて修正済み。
    """
    logging.info(f"動画ページからMP4 URLを検索中: {video_page_url}")
    try:
        async with httpx.AsyncClient() as client:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            response = await client.get(video_page_url, headers=headers, follow_redirects=True, timeout=30.0)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            # --- 抽出ロジック (tktube.com 向け) ---
            # JavaScript内の flashvars から抽出
            script_tags = soup.find_all('script')
            for script in script_tags:
                if script.string and 'flashvars' in script.string:
                    # 正規表現で video_alt_url (HD) または video_url (SD) を抽出
                    match_hd = re.search(r"video_alt_url:\s*'([^']+)'", script.string)
                    if match_hd:
                        mp4_url_raw = match_hd.group(1)
                        mp4_url = urldefrag(mp4_url_raw).url
                        logging.info(f"JavaScript flashvars (HD) からMP4 URLを発見: {mp4_url}")
                        # 相対URLの場合は絶対URLに変換
                        return urljoin(video_page_url, mp4_url).rstrip("/")

                    match_sd = re.search(r"video_url:\s*'([^']+)'", script.string)
                    if match_sd:
                        mp4_url_raw = match_sd.group(1)
                        mp4_url = urldefrag(mp4_url_raw).url
                        logging.info(f"JavaScript flashvars (SD) からMP4 URLを発見: {mp4_url}")
                        # 相対URLの場合は絶対URLに変換
                        return urljoin(video_page_url, mp4_url).rstrip("/")

            logging.warning(f"動画ページからMP4 URLが見つかりませんでした: {video_page_url}")
            return None
            # --- ここまで抽出ロジック ---

    except httpx.RequestError as e:
        logging.error(f"動画ページのHTML取得中にHTTPエラーが発生しました: {video_page_url} - {e}")
        return None
    except Exception as e:
        logging.error(f"MP4 URL検索中に予期せぬエラーが発生しました: {video_page_url} - {e}", exc_info=True)
        return None


# --- 動画ダウンロード処理 ---
async def download_video(
    mp4_url: str,
    output_path: str,
    progress_callback: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]] = None, # コールバックを Coroutine に変更
    chunk_size: int = 8192 # 8KB
) -> bool:
    """MP4ファイルを指定されたパスにダウンロードする (レジューム機能付き)""" # 説明を更新
    logging.info(f"ダウンロード開始: {mp4_url} -> {output_path}")
    total_size = 0
    downloaded_size = 0

    start_time = asyncio.get_event_loop().time()

    temp_output_path = output_path + ".part"

    # レジュームポイントを確認
    if os.path.exists(temp_output_path):
        downloaded_size = os.path.getsize(temp_output_path)
        logging.info(f"既存の部分ファイルを発見。レジュームします: {temp_output_path}, 既存サイズ: {downloaded_size} bytes")
        # progress_callback でレジューム開始を通知することも検討
        if progress_callback:
             await progress_callback({"status": "resuming", "downloaded_bytes": downloaded_size, "total_bytes": 0, "percentage": 0.0, "speed_bps": 0.0}) # レジューム開始ステータスを追加

    # 出力ディレクトリが存在しない場合は作成 (get_download_directory で作成済みだが念のため)
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        try:
            os.makedirs(output_dir)
            logging.info(f"出力ディレクトリを作成しました: {output_dir}")
        except OSError as e:
            logging.error(f"出力ディレクトリの作成に失敗しました: {output_dir} - {e}")
            if progress_callback:
                await progress_callback({"status": "error", "message": f"出力ディレクトリ作成失敗: {e}", "downloaded_bytes": downloaded_size, "total_bytes": 0})
            return False

    try:
        async with httpx.AsyncClient(timeout=None) as client: # タイムアウトを無効化 (大きなファイル用)
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                       'Referer': mp4_url} # Referer を追加 (必要になる場合がある)

            # レジューム用のRangeヘッダーを追加
            if downloaded_size > 0:
                 headers['Range'] = f'bytes={downloaded_size}-'
                 logging.debug(f"Rangeヘッダーを追加: {headers['Range']}")

            async with client.stream("GET", mp4_url, headers=headers, follow_redirects=True) as response:
                # レジューム時のステータスコード 206 Partial Content を考慮
                if response.status_code not in [200, 206]:
                     logging.error(f"ダウンロード中にHTTPステータスエラーが発生しました: {mp4_url} - Status: {response.status_code}")
                     if progress_callback:
                          await progress_callback({"status": "error", "message": f"HTTPエラー: {response.status_code}", "downloaded_bytes": downloaded_size, "total_bytes": 0})
                     return False

                # Content-Length または Content-Range から合計サイズを取得
                content_length = response.headers.get('content-length')
                content_range = response.headers.get('content-range')

                if content_range:
                     # Content-Range: bytes 0-1234/5678 の形式を解析
                     match = re.search(r'/(\d+)$', content_range)
                     if match:
                          total_size = int(match.group(1))
                          logging.info(f"Content-Rangeからファイルサイズを取得: {total_size} bytes")
                     else:
                          logging.warning(f"Content-Rangeヘッダーの解析に失敗しました: {content_range}")
                          # Content-Lengthがあればそちらを使うか、不明とする
                          if content_length:
                               total_size = int(content_length) + downloaded_size # レジューム時は既存サイズを足す
                               logging.info(f"Content-Range解析失敗、Content-Lengthからファイルサイズを推測: {total_size} bytes")
                          else:
                               logging.warning("ファイルサイズが不明です。進捗表示が不正確になります。")
                               total_size = 0 # サイズ不明
                elif content_length:
                     total_size = int(content_length)
                     if downloaded_size > 0:
                          total_size += downloaded_size # レジューム時は既存サイズを足す
                     logging.info(f"Content-Lengthからファイルサイズを取得: {total_size} bytes")
                else:
                     logging.warning("ファイルサイズが不明です。進捗表示が不正確になります。")
                     total_size = 0 # サイズ不明


                if progress_callback:
                    # 初期の進捗情報を送信 (レジューム開始時も含む)
                    await progress_callback({
                        "status": "downloading",
                        "downloaded_bytes": downloaded_size,
                        "total_bytes": total_size,
                        "percentage": (downloaded_size / total_size * 100) if total_size > 0 else 0.0,
                        "speed_bps": 0.0
                    })

                # ファイルを追記モードで開く
                try:
                    with open(temp_output_path, 'ab') as f: # 'ab' (append binary) モードに変更
                        async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                            if not chunk: # 空のチャンクが来たら終了？ (念のため)
                                break
                            f.write(chunk)
                            downloaded_size += len(chunk)

                            # 進捗更新は一定間隔で行う (例: 0.5秒ごと) # リアルタイム更新のため条件削除
                            if progress_callback: # and (current_time - last_progress_update_time > 0.5):
                                percentage = (downloaded_size / total_size * 100) if total_size > 0 else 0
                                elapsed_time = asyncio.get_event_loop().time() - start_time # 経過時間を毎回計算
                                speed_bps = (downloaded_size / elapsed_time) * 8 if elapsed_time > 0 else 0 # bits per second

                                await progress_callback({ # await を追加
                                    "status": "downloading",
                                    "downloaded_bytes": downloaded_size,
                                    "total_bytes": total_size,
                                    "percentage": round(percentage, 2),
                                    "speed_bps": round(speed_bps, 2)
                                })
                                # last_progress_update_time = current_time # リアルタイム更新のため削除
                            # UI更新のための短い待機（任意）
                            # await asyncio.sleep(0.01) # 必要に応じて追加

                    # ダウンロード完了後、一時ファイルをリネーム
                    # 既にファイルが存在する場合は上書き (レジューム完了時)
                    os.replace(temp_output_path, output_path) # os.rename より安全な os.replace を使用
                    logging.info(f"ダウンロード完了、一時ファイルをリネーム: {temp_output_path} -> {output_path}")


                except Exception as write_err:
                     logging.error(f"ファイル書き込み/リネーム中にエラー: {write_err}", exc_info=True)
                     # 一時ファイルを削除 (エラー発生時は削除しない方がレジュームしやすい場合も？今回は削除)
                     # if os.path.exists(temp_output_path):
                     #      os.remove(temp_output_path)
                     if progress_callback:
                          await progress_callback({"status": "error", "message": f"ファイル書き込みエラー: {write_err}", "downloaded_bytes": downloaded_size, "total_bytes": total_size})
                     return False


        # ダウンロードサイズの一致チェック (total_sizeが不明な場合はスキップ)
        if total_size > 0 and downloaded_size != total_size:
             logging.warning(f"ダウンロードサイズが一致しません: Expected={total_size}, Got={downloaded_size} for {output_path}")
             # 不完全なダウンロードとしてエラーにする
             if progress_callback:
                 await progress_callback({"status": "error", "message": "ダウンロードサイズ不一致", "downloaded_bytes": downloaded_size, "total_bytes": total_size})
             # 不完全なファイルを削除 (レジュームのために残すか検討)
             # if os.path.exists(output_path):
             #      os.remove(output_path)
             return False

        logging.info(f"ダウンロード完了: {output_path} ({downloaded_size} bytes)")
        if progress_callback:
            await progress_callback({ # await を追加
                "status": "finished",
                "downloaded_bytes": downloaded_size,
                "total_bytes": total_size,
                "percentage": 100.0, # 完了時は100%
                "speed_bps": 0.0 # 完了時は0
            })
        return True # 成功時は True を返す

    except httpx.RequestError as e: # HTTPStatusError も RequestError のサブクラスなのでこれでまとめて捕捉
        logging.error(f"ダウンロード中にHTTPリクエストエラーが発生しました: {mp4_url} - {e}")
        if progress_callback:
            # エラー発生時のステータスを failed_download に変更
            await progress_callback({"status": "failed_download", "message": f"HTTPリクエストエラー: {e}", "downloaded_bytes": downloaded_size, "total_bytes": total_size})
        return False
    except IOError as e:
         logging.error(f"ファイル操作エラーが発生しました: {output_path} - {e}")
         if progress_callback:
             await progress_callback({"status": "error", "message": f"ファイル操作エラー: {e}", "downloaded_bytes": downloaded_size, "total_bytes": total_size})
         return False
    except Exception as e:
        logging.error(f"ダウンロード中に予期せぬエラーが発生しました: {mp4_url} - {e}", exc_info=True)
        if progress_callback:
            await progress_callback({"status": "error", "message": f"予期せぬエラー: {e}", "downloaded_bytes": downloaded_size, "total_bytes": total_size})
        return False


# --- 統合関数 ---
async def download_video_from_page(
    video_page_url: str,
    output_filename: str, # ファイル名のみを受け取る
    output_directory: str, # 出力ディレクトリを別途指定
    progress_callback: Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]] = None # コールバックを Coroutine に変更
) -> Optional[str]: # 成功時はダウンロードしたファイルのパス、失敗時は None を返すように変更
    """動画ページURLからMP4 URLを見つけてダウンロードし、成功したらファイルパスを返す"""
    logging.info(f"動画ページからのダウンロード処理開始: {video_page_url}")

    # 1. 出力パスを構築 (安全なファイル名に変換)
    safe_filename = "".join(c if c.isalnum() or c in (' ', '.', '_', '-') else '_' for c in output_filename)
    if not safe_filename.lower().endswith('.mp4'):
        safe_filename += '.mp4'
    output_path = os.path.join(output_directory, safe_filename)
    logging.debug(f"構築された出力パス: {output_path}")

    # 2. MP4 URLを検索
    # レジュームダウンロードの場合でもMP4 URLは必要なので、毎回検索する
    mp4_url = await find_mp4_url(video_page_url)
    if not mp4_url:
        logging.error(f"MP4 URLの取得に失敗しました: {video_page_url}")
        if progress_callback:
            await progress_callback({"status": "error", "message": "MP4 URL取得失敗"}) # await を追加
        return None # 失敗時は None を返す
    logging.debug(f"取得したMP4 URL: {mp4_url}")


    # 3. 動画をダウンロード (レジューム機能付き)
    success = await download_video(mp4_url, output_path, progress_callback)

    if success:
         logging.info(f"ダウンロード処理完了 (成功): {output_path}")
         return output_path # 成功時はファイルパスを返す
    else:
         logging.error(f"ダウンロード処理完了 (失敗): {video_page_url}")
         return None # 失敗時は None を返す


# --- テスト用 ---
async def test_progress_callback(progress_info: Dict[str, Any]):
    """テスト用の進捗コールバック関数"""
    status = progress_info.get("status")
    if status == "downloading":
        # ダウンロード中の進捗表示
        downloaded = progress_info.get('downloaded_bytes', 0)
        total = progress_info.get('total_bytes', 0)
        percentage = progress_info.get('percentage', 0.0)
        speed = progress_info.get('speed_bps', 0.0)
        print(f"\rDownloading... {percentage:.2f}% ({downloaded}/{total}) "
              f"Speed: {speed:.2f} bps", end="", flush=True)
    elif status == "finished":
        print("\nDownload finished!")
    elif status == "error":
        print(f"\nDownload error: {progress_info.get('message')}")
    elif status == "resuming": # レジューム開始ステータスを追加
        print(f"\nResuming download from {progress_info.get('downloaded_bytes')} bytes...")
    else:
        print(f"\nStatus update: {status} - {progress_info.get('message', '')}")


async def main_test():
    # テスト用の動画ページURL (実際のURLに置き換える必要あり)
    test_video_page_url = "https://tktube.com/ja/videos/314819/fc2-ppv-4670832-4-27-21/" # ログにあったURL
    test_output_filename = "FC2-PPV-4670832 Test Video" # タイトルから生成される想定
    # test_output_dir = "download_test" # テスト用出力ディレクトリ
    test_output_dir = get_download_directory() # OS標準ダウンロードディレクトリを使用

    print(f"テストダウンロードを開始します: {test_video_page_url}")
    downloaded_path = await download_video_from_page(
        test_video_page_url,
        test_output_filename,
        test_output_dir,
        progress_callback=test_progress_callback
    )

    if downloaded_path:
        print(f"テストダウンロードが成功しました。ファイル: {downloaded_path}")
    else:
        print("テストダウンロードが失敗しました。")

if __name__ == '__main__':
    # web_app から実行されるので、ここでのロギング設定は不要になることが多い
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s [%(funcName)s] - %(message)s')
    asyncio.run(main_test())
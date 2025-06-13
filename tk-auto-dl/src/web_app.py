import asyncio
import json
from typing import Optional, List, Dict, Any # 型ヒント用

import logging
import os
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi import WebSocket, WebSocketDisconnect # WebSocket関連のインポートを追加
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse # StreamingResponse をインポート
from fastapi.staticfiles import StaticFiles # 静的ファイル配信用
from fastapi.templating import Jinja2Templates # HTMLテンプレート用
from contextlib import asynccontextmanager # lifespan用 (FastAPI 0.90.0+)
from datetime import datetime, timedelta # 巡回期間制限用
import re # タスク名からIDを抽出するために追加

# 作成したモジュールをインポート
from .status_manager import StatusManager
from .content_scraper import scrape_eligible_videos
from .download_module import download_video_from_page, get_download_directory # get_download_directory をインポート
from .llm_tts_service import process_comment_for_audio
# from .upload_module import upload_to_server # アップロード機能削除のためコメントアウト

# ロギング設定 (DEBUG レベルで詳細を確認)
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')

# --- グローバル変数 ---
# StatusManager のインスタンス
status_manager = StatusManager()

# バックグラウンドタスク管理用
background_tasks_running = False
stop_requested_flag = False # アプリケーションレベルでの停止フラグ
main_task_handle: Optional[asyncio.Task] = None

# 同時実行数制御
MAX_CONCURRENT_DOWNLOADS = 8 # 5から8に変更
# MAX_CONCURRENT_UPLOADS = 8 # アップロード機能削除のため削除
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
# upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS) # アップロード機能削除のため削除

# 追加要件用定数
MAX_QUEUE_SIZE = 20

# --- FastAPI アプリケーション設定 ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("アプリケーションを起動します...")
    global stop_requested_flag # グローバル変数にアクセスするために必要
    stop_requested_flag = False # アプリケーション起動時に停止フラグをリセット
    await status_manager.clear_stop_request() # StatusManager内の停止フラグもクリア

    # アプリケーション起動時のタスクステータスリセットは start エンドポイントに移動
    # await status_manager.reset_state_async()

    yield # ここでアプリケーションが起動し、リクエスト処理などが可能になる

    logging.info("アプリケーションをシャットダウンします...")
    global main_task_handle # シャットダウン処理で必要
    stop_requested_flag = True # シャットダウン時に停止フラグを立てる
    if main_task_handle and not main_task_handle.done():
        logging.info("バックグラウンドタスクの完了を待機中...")
        try:
            await asyncio.wait_for(main_task_handle, timeout=10.0)
        except asyncio.TimeoutError:
            logging.warning("バックグラウンドタスクのシャットダウンがタイムアウトしました。")
        except asyncio.CancelledError:
             logging.info("バックグラウンドタスクはキャンセルされました。")

    # アプリケーションシャットダウン時に状態を保存
    logging.info("シャットダウン前にタスク状態を保存します。")
    await status_manager._save_status()
    logging.info("タスク状態の保存が完了しました。")

    logging.info("アプリケーションがシャットダウンしました。")


app = FastAPI(lifespan=lifespan)

# --- 静的ファイルとテンプレートの設定 ---
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
if not os.path.isdir(templates_dir):
     logging.warning(f"Templates directory not found at: {templates_dir}. Creating it.")
     try:
         os.makedirs(templates_dir)
     except OSError as e:
         logging.error(f"Failed to create templates directory: {e}")
else:
     templates = Jinja2Templates(directory=templates_dir)


# --- バックグラウンドワーカー ---

async def download_worker(fc2_id: str, task_info: dict):
    """ダウンロードタスクを実行するワーカー"""
    async with download_semaphore:
        if stop_requested_flag:
            logging.info(f"停止リクエスト検出のため、ダウンロードをスキップ: {fc2_id}")
            return

        logging.info(f"ダウンロードワーカー開始: {fc2_id}")
        video_page_url = task_info.get("video_page_url")
        title = task_info.get("title", fc2_id)
        # output_dir = "downloads" # OS標準ダウンロードディレクトリを使用するため変更
        output_dir = get_download_directory() # download_module からディレクトリパスを取得
        # os.makedirs(output_dir, exist_ok=True) # get_download_directory で作成済み

        async def progress_callback(progress_data: Dict[str, Any]):
            await status_manager.update_download_progress(fc2_id, progress_data)

        success = False
        local_path = None
        try:
            # download_video_from_page は成功時にローカルパスを返すように変更が必要かもしれない
            # 現状は成功フラグのみと仮定
            result = await download_video_from_page(
                video_page_url,
                output_filename=title,
                output_directory=output_dir,
                progress_callback=progress_callback
            )
            # download_video_from_page がパスを返す場合
            if isinstance(result, str) and os.path.exists(result):
                 success = True
                 local_path = result
            # download_video_from_page がブール値を返す場合 (旧仕様)
            elif isinstance(result, bool) and result:
                 success = True
                 # この場合、ファイル名を推測する必要がある
                 safe_filename = "".join(c if c.isalnum() or c in (' ', '.', '_', '-') else '_' for c in title)
                 if not safe_filename.lower().endswith('.mp4'):
                      safe_filename += '.mp4'
                 local_path = os.path.join(output_dir, safe_filename)
                 if not os.path.exists(local_path):
                      logging.error(f"ダウンロード成功と報告されましたが、ファイルが見つかりません: {local_path}")
                      success = False # ファイルがないので失敗扱い

            if success and local_path:
                 logging.info(f"ダウンロード成功、ローカルパス: {local_path}")
                 await status_manager.set_download_local_path(fc2_id, local_path)
                 # ダウンロード完了状態への更新 (アップロードキュー追加含む)
                 # アップロードキューへの追加は不要になったため、ダウンロード完了状態のみ更新
                 await status_manager.update_download_progress(fc2_id, {"status": "finished", "local_path": local_path})
            else:
                 logging.error(f"ダウンロードワーカー失敗 (download_video_from_page から false または無効なパス): {fc2_id}")
                 # status_manager 側でエラー状態にする (progress_callback経由でなければ)
                 task_status = await status_manager.get_task_status(fc2_id) # status_manager.py で実装済み
                 if task_status and not task_status.get('status', '').startswith('failed'):
                      await status_manager.update_download_progress(fc2_id, {"status": "error", "message": "ダウンロード処理失敗"})

        except Exception as e:
            logging.error(f"ダウンロードワーカー実行中に予期せぬエラー: {fc2_id} - {e}", exc_info=True)
            await status_manager.update_download_progress(fc2_id, {"status": "error", "message": f"予期せぬエラー: {e}"})


# async def upload_worker(fc2_id: str, task_info: dict): # アップロード機能削除のため削除
#     """アップロードタスクを実行するワーカー"""
#     async with upload_semaphore:
#         if stop_requested_flag:
#             logging.info(f"停止リクエスト検出のため、アップロードをスキップ: {fc2_id}")
#             return
#
#         logging.info(f"アップロードワーカー開始: {fc2_id}")
#         local_path = task_info.get("local_path")
#         title = task_info.get("title")
#
#         if not local_path or not title:
#             logging.error(f"アップロード情報不足 (ローカルパスまたはタイトル): {fc2_id}")
#             await status_manager.update_upload_progress(fc2_id, {"status": "error", "message": "アップロード情報不足"})
#             return
#         if not os.path.exists(local_path):
#              logging.error(f"アップロード対象のローカルファイルが見つかりません: {local_path}")
#              await status_manager.update_upload_progress(fc2_id, {"status": "error", "message": "ローカルファイル不明"})
#              return
#
#         async def progress_callback(progress_data: Dict[str, Any]):
#             await status_manager.update_upload_progress(fc2_id, progress_data)
#
#         try:
#             success = await upload_to_server(
#                 local_path,
#                 title,
#                 progress_callback=progress_callback
#             )
#
#             if success:
#                 logging.info(f"アップロードワーカー完了 (成功またはスキップ): {fc2_id}")
#                 # アップロード成功後、StatusManagerにローカルファイルの削除を依頼
#                 await status_manager.delete_local_file(fc2_id)
#             else:
#                 # upload_to_server が False を返した場合 (progress_callback でエラーになっていない場合)
#                 logging.error(f"アップロードワーカー失敗 (upload_to_server から False): {fc2_id}")
#                 task_status = await status_manager.get_task_status(fc2_id) # status_manager.py で実装済み
#                 if task_status and not task_status.get('status', '').startswith('failed'):
#                      await status_manager.update_upload_progress(fc2_id, {"status": "error", "message": "アップロード処理失敗"})
#
#         except Exception as e:
#             logging.error(f"アップロードワーカー実行中に予期せぬエラー: {fc2_id} - {e}", exc_info=True)
#             await status_manager.update_upload_progress(fc2_id, {"status": "error", "message": f"予期せぬエラー: {e}"})
#
#
async def main_background_loop():
    """メインのバックグラウンド処理ループ"""
    global background_tasks_running, stop_requested_flag
    logging.info("メインバックグラウンドループを開始します。")
    logging.debug(f"main_background_loop 開始時のstop_requested_flag: {stop_requested_flag}") # デバッグログ追加
    logging.debug(f"main_background_loop 開始時のbackground_tasks_running: {background_tasks_running}") # デバッグログ追加
    background_tasks_running = True
    stop_requested_flag = False # 開始時にフラグをクリア

    try:
        # スタート時にダウンロードディレクトリをチェックし、レジューム可能なタスクを探す
        # Auto Start 時にリセットではなくレジュームを行うため、reset_state_async() は削除
        # await status_manager.check_and_resume_downloads("downloads") # start エンドポイントに移動

        # 1. スクレイピング実行
        logging.info("スクレイピングを開始します...")
        # 既に処理済みのIDを取得 (完了、スキップ、エラーなど)
        processed_ids = await status_manager.get_processed_ids()
        # task_status から完了済みのIDも取得して除外対象に含める
        all_tasks = await status_manager.get_all_status()
        completed_ids = {fc2_id for fc2_id, task_info in all_tasks["task_status"].items() if task_info.get("status") == "completed"}
        
        # processed_ids と completed_ids を結合して、スクレイピングの除外対象とする
        exclude_ids = processed_ids.union(completed_ids)

        from .content_scraper import TARGET_URL, REQUIRED_DAYS_PASSED
        one_month_ago = datetime.now() - timedelta(days=30)
        eligible_videos = await scrape_eligible_videos(
            TARGET_URL,
            exclude_ids, # 処理済みのIDを除外
            max_pages=5,
            oldest_date=one_month_ago
        )

        if stop_requested_flag:
             logging.info("スクレイピング後に停止リクエストを検出。")
             raise asyncio.CancelledError

        # 2. ダウンロード対象をキューに追加 (キューサイズ制限付き)
        logging.info(f"{len(eligible_videos)} 件の動画が見つかりました。キューに追加します (最大{MAX_QUEUE_SIZE}件)...")
        added_count = 0
        async with status_manager._lock:
            current_dl_queue_size = len(status_manager.download_queue)
            available_slots = MAX_QUEUE_SIZE - current_dl_queue_size
        
        logging.debug(f"現在のダウンロードキューサイズ: {current_dl_queue_size}, 追加可能数: {available_slots}")

        if available_slots > 0:
            for video in eligible_videos[:available_slots]:
                await status_manager.add_download_task(video)
                added_count += 1
                if stop_requested_flag: break
            logging.info(f"{added_count} 件をダウンロードキューに追加しました。")
        else:
            logging.info("ダウンロードキューが満杯のため、今回はタスクを追加しません。")


        if stop_requested_flag:
             logging.info("タスク追加後に停止リクエストを検出。")
             raise asyncio.CancelledError

        # ★★★ ログ追加 ★★★
        logging.info("キュー追加完了、ワーカー実行ループへ移行します。")

        # 3. ダウンロードワーカーの実行ループ (アップロードワーカーは削除)
        active_workers: List[asyncio.Task] = []
        logging.info("--- ワーカー実行ループ開始 ---")
        logging.debug(f"main_background_loop 開始時のstop_requested_flag: {stop_requested_flag}") # デバッグログ追加
        logging.debug(f"main_background_loop 開始時のbackground_tasks_running: {background_tasks_running}") # デバッグログ追加
        loop_count = 0
        while True:
            loop_count += 1
            logging.debug(f"--- ワーカー実行ループ {loop_count} 回目開始 ---") # デバッグログ追加
            logging.debug(f"ループ開始時のstop_requested_flag: {stop_requested_flag}") # デバッグログ追加

            if stop_requested_flag:
                logging.info("ワーカー実行ループで停止リクエストを検出。")
                break

            # --- 新しいダウンロードタスクを開始 ---
            current_download_workers = [t for t in active_workers if not t.done() and "download" in t.get_name()]
            logging.debug(f"現在のダウンロードワーカー数: {len(current_download_workers)} / {MAX_CONCURRENT_DOWNLOADS}")
            if len(current_download_workers) < MAX_CONCURRENT_DOWNLOADS:
                async with status_manager._lock: # キューの状態を確認
                    current_dl_queue_ids = status_manager.download_queue.copy()
                    logging.debug(f"次のDLタスク取得試行前 - キュー内容 ({len(current_dl_queue_ids)}件): {list(current_dl_queue_ids)}") # デバッグログ追加

                next_dl_task_result = await status_manager.get_next_download_task()

                if next_dl_task_result:
                    fc2_id, next_dl_task_info = next_dl_task_result
                    logging.info(f"ダウンロードタスク取得成功: {fc2_id}")
                    logging.debug(f"取得したタスク情報: {next_dl_task_info}")
                    if not next_dl_task_info or not isinstance(next_dl_task_info, dict):
                         logging.error(f"取得したタスク情報が無効です: {fc2_id}, Info: {next_dl_task_info}")
                         continue # 次のループへ

                    logging.info(f"ダウンロードワーカータスクを作成中: {fc2_id}")
                    try:
                        worker_task = asyncio.create_task(download_worker(fc2_id, next_dl_task_info), name=f"download-{fc2_id}")
                        active_workers.append(worker_task)
                        logging.info(f"ダウンロードワーカータスクを作成しました: {worker_task.get_name()}")
                    except Exception as e_create:
                         logging.error(f"ダウンロードワーカータスク作成中にエラー: {fc2_id} - {e_create}", exc_info=True)
                else:
                    logging.debug("get_next_download_task が None を返しました (キューが空 or 内部エラー)。")
            else:
                 logging.debug("ダウンロードワーカーが最大数に達しています。")


            # --- 完了したワーカーをリストから削除 ---
            done_workers = [t for t in active_workers if t.done()]
            if done_workers:
                 logging.debug(f"{len(done_workers)} 件の完了したワーカーを検出。")
                 for t in done_workers:
                     try:
                         t.result()
                         logging.debug(f"ワーカー {t.get_name()} は正常に完了しました。")
                     except asyncio.CancelledError:
                         logging.debug(f"ワーカー {t.get_name()} はキャンセルされました。")
                     except Exception as e_done:
                         logging.error(f"ワーカー {t.get_name()} で例外が発生しました: {e_done}", exc_info=True)

            active_workers = [t for t in active_workers if not t.done()]
            logging.debug(f"アクティブなワーカー数 (完了削除後): {len(active_workers)}")

            # --- 終了条件のチェック ---
            current_status = await status_manager.get_all_status()
            # アップロードキューの表示を削除
            logging.debug(f"現在のキュー状況 (終了チェック前): DL={current_status['download_queue_count']}") # デバッグログ追加
            logging.debug(f"アクティブなワーカー数 (終了チェック前): {len(active_workers)}") # デバッグログ維持
            logging.debug(f"停止リクエストフラグ (終了チェック前): {stop_requested_flag}") # デバッグログ維持

            # 終了条件のチェック: アクティブなワーカーがいない場合のみループを終了
            if not active_workers:
                logging.info("アクティブなワーカーがいません。メインバックグラウンドループを終了します。")
                break
            
            # Stopリクエストがあった場合、キューが空でなくてもループを終了
            if stop_requested_flag:
                 logging.info("停止リクエストが検出されたため、ワーカー実行ループを終了します。")
                 break


            logging.debug("ループ待機 (1秒)...")
            await asyncio.sleep(1)

    except asyncio.CancelledError:
        logging.info("メインバックグラウンドループがキャンセルされました。")
        # 実行中のワーカーをキャンセルし、状態をpausedに更新
        tasks_to_cancel = [t for t in active_workers if not t.done()] # キャンセル対象のタスクリスト
        logging.info(f"{len(tasks_to_cancel)} 件の実行中タスクを中断します。")
        
        paused_tasks_ids = [] # pausedに更新するタスクのIDリスト
        for task in tasks_to_cancel:
            task.cancel()
            # タスク名からFC2 IDを抽出してpaused_tasks_idsに追加
            task_name = task.get_name()
            # アップロードタスクの正規表現を削除
            match = re.match(r"(download)-(FC2-PPV-\d+)", task_name)
            if match:
                task_type = match.group(1)
                fc2_id = match.group(2)
                paused_tasks_ids.append((fc2_id, task_type)) # (ID, タイプ) のタプルで保存
            else:
                logging.warning(f"タスク名 '{task_name}' からFC2 IDを抽出できませんでした。状態を更新できません。")

        # キャンセルされたタスクが実際に終了するまで待機
        if tasks_to_cancel:
             logging.info("実行中のワーカーの終了を待機中...")
             await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
             logging.info("実行中のワーカーの終了待機が完了しました。")

        # 終了待機後に、paused_tasks_idsリストを使って状態を更新
        for fc2_id, task_type in paused_tasks_ids:
             logging.info(f"タスク {fc2_id} ({task_type}) をpaused状態に更新します。")
             # StatusManagerを使って状態を更新
             if task_type == "download":
                 await status_manager.update_download_progress(fc2_id, {"status": "paused", "message": "中断されました"})
             # elif task_type == "upload": # アップロード機能削除のため削除
             #     await status_manager.update_upload_progress(fc2_id, {"status": "paused", "message": "中断されました"})


    except Exception as e:
        logging.error(f"メインバックグラウンドループで予期せぬエラーが発生しました: {e}", exc_info=True)
    finally:
        logging.info("メインバックグラウンドループが終了しました。")
        background_tasks_running = False
        stop_requested_flag = False # 終了時にフラグをクリア


# --- API エンドポイント ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """フロントエンドのHTMLページを返す"""
    if 'templates' not in globals():
         return HTMLResponse("<html><body>Template engine not configured.</body></html>")
    context = {"request": request, "title": "tk-auto-dl"}
    return templates.TemplateResponse("index.html", context)

# 既存の /status エンドポイントは削除またはコメントアウト
# @app.get("/status")
# async def get_status():
#     """現在の処理状況を返す"""
#     current_status = await status_manager.get_all_status()
#     return JSONResponse(content={
#         "background_running": background_tasks_running,
#         "stop_requested": stop_requested_flag,
#         **current_status
#     })

@app.get("/status-stream")
async def status_stream(request: Request):
    """現在の処理状況をSSEでストリーム配信する"""
    async def event_generator():
        while True:
            # クライアントが切断されたかチェック
            if await request.is_disconnected():
                logging.info("SSE クライアントが切断されました。")
                break

            # StatusManager の状態更新を待機
            await status_manager.wait_for_status_update()

            # 最新のステータスを取得
            current_status = await status_manager.get_all_status()

            # データをSSEフォーマットで送信
            yield f"data: {json.dumps(current_status)}\n\n"

            # 短時間待機してCPU使用率を抑える (必須ではないが推奨)
            await asyncio.sleep(0.1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            logging.debug(f"受信したWebSocketメッセージ (raw data): {data}")
            try:
                message = json.loads(data)
                if message.get("type") == "comments" and "data" in message:
                    comments_data = message["data"].get("comments", [])
                    for comment_detail in comments_data:
                        # ログから get_comment_details の呼び出しを模倣
                        # 実際の get_comment_details 関数は llm_tts_service.py にはないため、ここで直接データを抽出
                        comment_id = comment_detail.get("id")
                        comment_text = comment_detail.get("data", {}).get("comment")
                        
                        if comment_id and comment_text:
                            logging.debug(f"新しいコメントを受信: ID={comment_id}, Comment='{comment_text[:20]}...'")
                            # コメント処理をバックグラウンドで実行
                            asyncio.create_task(process_comment_for_audio(comment_id, comment_text))
                        else:
                            logging.warning(f"無効なコメントデータを受信しました: {comment_detail}")
                else:
                    logging.warning(f"不明なWebSocketメッセージタイプを受信しました: {message.get('type')}")
            except json.JSONDecodeError:
                logging.error(f"受信したWebSocketメッセージがJSON形式ではありません: {data}")
            except Exception as e:
                logging.error(f"WebSocketメッセージ処理中にエラーが発生しました: {e}", exc_info=True)
    except WebSocketDisconnect:
        logging.info("WebSocketクライアントが切断されました。")
    except Exception as e:
        logging.error(f"WebSocket接続中に予期せぬエラーが発生しました: {e}", exc_info=True)


@app.post("/start")
async def start_processing(background_tasks: BackgroundTasks):
    """バックグラウンド処理を開始する"""
    global background_tasks_running, stop_requested_flag, main_task_handle # stop_requested_flag を追加

    # Auto Start が押されたら、まず停止フラグを強制的に解除
    logging.info("Auto Start リクエスト: 停止フラグを強制解除します。") # ログ追加
    stop_requested_flag = False
    await status_manager.clear_stop_request()
    logging.debug(f"停止フラグ解除後のstop_requested_flag: {stop_requested_flag}") # デバッグログ追加


    if background_tasks_running:
        raise HTTPException(status_code=400, detail="処理は既に実行中です。")

    logging.info("バックグラウンド処理の開始リクエストを受け付けました。")
    logging.debug(f"start_processing 実行時のstop_requested_flag: {stop_requested_flag}") # デバッグログ維持
    logging.debug(f"start_processing 実行時のbackground_tasks_running: {background_tasks_running}") # デバッグログ維持

    # Auto Start 時にリセットではなくレジュームを行うため、reset_state_async() は削除
    # await status_manager.reset_state_async() # 削除済み

    # ダウンロードディレクトリをチェックし、既存ファイルに基づいてタスク状態を更新
    # await status_manager.check_and_resume_downloads("downloads") # ここを修正
    download_dir = get_download_directory() # download_module からディレクトリパスを取得
    await status_manager.check_and_resume_downloads(download_dir) # 取得したパスを使用

    # 中断されたタスクをレジュームキューに戻す
    await status_manager.resume_paused_tasks()

    main_task_handle = asyncio.create_task(main_background_loop(), name="main_loop")
    return JSONResponse(content={"message": "バックグラウンド処理を開始しました。"})

@app.post("/stop")
async def stop_processing():
    """バックグラウンド処理の中断をリクエストする"""
    global stop_requested_flag, main_task_handle # main_task_handle を追加
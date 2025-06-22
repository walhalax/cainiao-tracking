import asyncio
import json
import logging
import os
from typing import Dict, Any, Optional, Set, Tuple, List
from collections import deque
from datetime import datetime
import re # FC2 ID 抽出用にインポート

# ロギング設定
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')

STATUS_FILE = "task_status.json"

class StatusManager:
    def __init__(self, status_file=STATUS_FILE):
        self.status_file = status_file
        self._lock = asyncio.Lock()
        # メモリ上の状態 (ファイルは永続化用)
        self.task_status: Dict[str, Dict[str, Any]] = {} # {fc2_id: {"status": "...", "progress": ..., ...}}
        self.download_queue: deque[str] = deque() # ダウンロード待ちの fc2_id
        # self.upload_queue: deque[str] = deque()   # アップロード機能削除のため削除
        self.processed_ids: Set[str] = set()      # 完了/スキップ済みの fc2_id
        self.stop_requested: bool = False         # 停止リクエストフラグ

        # 状態更新通知のためのイベント
        self._status_updated_event = asyncio.Event()

        # 起動時にファイルから状態を読み込む
        asyncio.create_task(self._load_status())

    async def _load_status(self):
        """状態ファイルからステータスを読み込む"""
        async with self._lock:
            logging.debug("状態ファイルの読み込みを開始します。") # デバッグログ追加
            if os.path.exists(self.status_file):
                try:
                    with open(self.status_file, 'r') as f:
                        data = json.load(f)
                        self.task_status = data.get('task_status', {})
                        self.download_queue = deque(data.get('download_queue', []))
                        # self.upload_queue = deque(data.get('upload_queue', [])) # アップロード機能削除のため削除
                        self.processed_ids = set(data.get('processed_ids', []))
                        # アップロードキューの表示を削除
                        logging.info(f"状態ファイルを読み込みました: {len(self.task_status)} tasks, {len(self.download_queue)} DL queue, {len(self.processed_ids)} processed.")
                        # 整合性チェック (キューにあるIDがtask_statusに存在するかなど) を追加しても良い
                        logging.debug(f"読み込み後のtask_status: {json.dumps(self.task_status, indent=2)}") # デバッグログ維持
                        logging.debug(f"読み込み後のdownload_queue: {list(self.download_queue)}") # デバッグログ維持
                        # logging.debug(f"読み込み後のupload_queue: {list(self.upload_queue)}") # アップロード機能削除のため削除
                        # 各タスクの状態を個別にログ出力 (詳細)
                        for fc2_id, task_info in self.task_status.items():
                             # アップロード進捗の表示を削除
                             logging.debug(f"読み込みタスク状態: {fc2_id} - Status: {task_info.get('status')}, LocalPath: {task_info.get('local_path')}, DL_Progress: {task_info.get('download_progress')}") # デバッグログ追加

                except (json.JSONDecodeError, IOError) as e:
                    logging.error(f"状態ファイルの読み込みに失敗しました: {e}. 新しい状態で開始します。")
                    self._reset_state() # エラー時はリセット
            else:
                logging.info("状態ファイルが見つかりません。新しい状態で開始します。")
                self._reset_state()
            logging.debug("状態ファイルの読み込みを完了しました。") # デバッグログ追加
            self._status_updated_event.set() # 読み込み完了時にもイベントをセット

    async def _save_status(self):
        """現在のステータスを状態ファイルに保存する"""
        # ロックは呼び出し元で取得されている想定
        logging.debug("状態ファイルの保存を開始します。") # デバッグログ追加
        try:
            data_to_save = {
                'task_status': self.task_status,
                'download_queue': list(self.download_queue),
                # 'upload_queue': list(self.upload_queue), # アップロード機能削除のため削除
                'processed_ids': list(self.processed_ids)
            }
            with open(self.status_file, 'w') as f:
                json.dump(data_to_save, f, indent=4)
            logging.debug("状態をファイルに保存しました。") # デバッグログ維持
        except IOError as e:
            logging.error(f"状態ファイルの保存に失敗しました: {e}")
        logging.debug("状態ファイルの保存を完了しました。") # デバッグログ追加
        self._status_updated_event.set() # 保存完了時にもイベントをセット


    def _reset_state(self):
        """メモリ上の状態をリセットする (非同期ではない内部用)"""
        logging.debug("メモリ上の状態をリセットします。") # デバッグログ追加
        self.task_status = {}
        self.download_queue = deque()
        # self.upload_queue = deque() # アップロード機能削除のため削除
        self.processed_ids = set()
        self.stop_requested = False
        logging.debug("メモリ上の状態のリセットが完了しました。") # デバッグログ追加


    async def reset_state_async(self):
        """メモリ上の状態をリセットし、ファイルに保存する (外部呼び出し用)"""
        async with self._lock:
            logging.info("タスクステータスをリセットします。") # ログ維持
            self._reset_state()
            await self._save_status()
            logging.info("タスクステータスのリセットが完了しました。") # ログ維持
        self._status_updated_event.set() # リセット完了時にもイベントをセット


    async def add_download_task(self, video_info: Dict[str, Any]):
        """新しいダウンロードタスクをキューとステータスに追加する"""
        fc2_id = video_info.get('fc2_id')
        if not fc2_id:
            logging.warning("FC2 ID がないためタスクを追加できません。") # ログ維持
            return

        async with self._lock:
            logging.debug(f"ダウンロードタスク追加処理開始: {fc2_id}") # デバッグログ追加

            # 既存のタスクがあるかチェック
            existing_task = self.task_status.get(fc2_id)
            if existing_task:
                # 既に完了している場合はスキップ
                if existing_task.get("status") == "completed" or fc2_id in self.processed_ids: # processed_ids もチェック
                    logging.info(f"タスク {fc2_id} は既に完了しています (DBまたはprocessed_ids)。スキップします。")
                    return
                # ダウンロード中またはダウンロード待ちの場合はスキップ
                if existing_task.get("status") in ["pending_download", "downloading"]:
                    logging.debug(f"タスク {fc2_id} は既にダウンロード待ちまたはダウンロード中です。スキップ。")
                    return

            # ローカルファイルが存在しない、またはサイズが0の場合、ダウンロードタスクとして追加/更新
            # タスクが task_status に存在しない、または状態が pending_download/downloading でない場合
            # ここは既存タスクチェックでカバーされるため、条件を簡略化
            logging.info(f"タスク {fc2_id} をダウンロードキューに追加/更新します。") # ログ維持
            self.task_status[fc2_id] = {
                "status": "pending_download",
                "title": video_info.get('title'),  # 修正不要 - 既にvideo_infoから取得
                "url": video_info.get('url'),
                "added_date": video_info.get('added_date_str'),
                "rating": video_info.get('rating'),
                "download_progress": 0,
                # "upload_progress": 0, # アップロード機能削除のため削除
                "local_path": None, # 新規ダウンロードの場合はローカルパスをリセット
                "error_message": None,
                "last_updated": datetime.now().isoformat() # ここで datetime を使用
            }
            # processed_ids に含まれている場合は削除
            if fc2_id in self.processed_ids:
                self.processed_ids.remove(fc2_id)
                logging.debug(f"タスク {fc2_id} をprocessed_idsから削除しました。") # デバッグログ追加

            # ダウンロードキューに存在しない場合のみ追加
            if fc2_id not in self.download_queue:
                self.download_queue.append(fc2_id)
                logging.info(f"ダウンロードキューに追加: {fc2_id} - {video_info.get('title')}") # video_infoから直接取得
            else:
                logging.debug(f"タスク {fc2_id} は既にダウンロードキューに存在します。スキップ。") # デバッグログ維持

            await self._save_status() # 状態を保存
            logging.debug(f"タスク {fc2_id} をダウンロードキューに追加/更新し、状態を保存しました。") # デバッグログ追加
            logging.debug(f"ダウンロードタスク追加処理完了: {fc2_id}") # デバッグログ追加
        self._status_updated_event.set() # 状態変更時にイベントをセット


    async def get_next_download_task(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        """ダウンロードキューから次のタスクを取得し、状態を 'downloading' に更新する"""
        async with self._lock:
            logging.debug("次のダウンロードタスク取得処理開始。") # デバッグログ追加
            if self.stop_requested:
                logging.debug("停止リクエスト中のため、新しいダウンロードタスクは取得しません。") # ログ維持
                logging.debug("次のダウンロードタスク取得処理完了 (停止リクエスト)。") # デバッグログ追加
                return None
            if not self.download_queue:
                logging.debug("ダウンロードキューは空です。") # ログ維持
                logging.debug("次のダウンロードタスク取得処理完了 (キュー空)。") # デバッグログ追加
                return None

            fc2_id = self.download_queue.popleft()
            logging.debug(f"キューからタスク {fc2_id} を取得しました。") # デバッグログ追加
            if fc2_id in self.task_status:
                self.task_status[fc2_id].update({
                    "status": "downloading",
                    "download_progress": 0, # 開始時にリセット
                    "error_message": None,
                    "last_updated": datetime.now().isoformat()
                })
                logging.info(f"次のダウンロードタスクを取得: {fc2_id}") # ログ維持
                task_info = self.task_status[fc2_id]
                await self._save_status() # 状態を保存
                logging.debug(f"タスク {fc2_id} の状態をdownloadingに更新し、保存しました。") # デバッグログ追加
                # タスク情報に必要なキーを追加 (ワーカーが必要とする情報)
                task_info_for_worker = {
                    "title": task_info.get("title"),
                    "video_page_url": task_info.get("url") # download_module が使うURL
                }
                logging.debug(f"次のダウンロードタスク取得処理完了: {fc2_id}") # デバッグログ追加
                return fc2_id, task_info_for_worker
            else:
                logging.warning(f"キューにあったID {fc2_id} が task_status に存在しません。") # ログ維持
                # キューから削除されたので、状態保存は不要
                logging.debug("次のダウンロードタスク取得処理完了 (タスク不明)。") # デバッグログ追加
                return None # 見つからない場合はNoneを返す


    async def update_download_progress(self, fc2_id: str, progress_data: Dict[str, Any]):
        """ダウンロードの進捗や状態を更新する"""
        async with self._lock:
            logging.debug(f"ダウンロード進捗更新処理開始: {fc2_id}") # デバッグログ追加
            if fc2_id in self.task_status:
                current_task = self.task_status[fc2_id]
                # Progressデータ全体を更新しつつ、percentageをdownload_progressに明示的に設定
                current_task.update(progress_data)
                current_task["download_progress"] = progress_data.get("percentage", 0) # percentage を保存
                current_task["last_updated"] = datetime.now().isoformat()

                new_status = progress_data.get("status")
                logging.debug(f"ダウンロード進捗更新: {fc2_id} - Status: {new_status}, Data: {progress_data}") # ログ維持

                if new_status == "finished":
                    logging.info(f"ダウンロード完了: {fc2_id}") # ログ維持
                    # 完了したらprocessed_idsに追加
                    self.processed_ids.add(fc2_id)
                    current_task["status"] = "completed" # ステータスを更新
                    logging.debug(f"ダウンロード完了処理: {fc2_id} - processed_idsに追加しました。") # デバッグログ追加

                elif new_status == "error" or new_status == "failed_download":
                    logging.error(f"ダウンロード失敗/エラー: {fc2_id} - {progress_data.get('message')}") # ログ維持
                    # processed_ids には追加しない (リセット可能にするため)
                    logging.debug(f"ダウンロード失敗処理: {fc2_id} - processed_idsに追加しません。") # デバッグログ追加
                elif new_status == "skipped":
                     logging.info(f"ダウンロードスキップ: {fc2_id} - {progress_data.get('message')}") # ログ維持
                     self.processed_ids.add(fc2_id) # スキップは処理済みとする
                     # task_status から削除するかどうか？ 일단残す
                     logging.debug(f"ダウンロードスキップ処理: {fc2_id} - processed_idsに追加しました。") # デバッグログ追加
                elif new_status == "paused": # paused 状態の更新を追加
                     logging.info(f"ダウンロード中断: {fc2_id} - {progress_data.get('message')}") # ログ追加
                     current_task["status"] = "paused"
                     logging.debug(f"ダウンロード中断処理: {fc2_id} - 状態をpausedに更新しました。") # デバッグログ追加


                await self._save_status() # 状態を保存
                logging.debug(f"ダウンロード進捗更新処理完了: {fc2_id} - 状態を保存しました。") # デバッグログ追加
            else:
                logging.warning(f"進捗更新対象のタスクが見つかりません: {fc2_id}") # ログ維持
                logging.debug(f"ダウンロード進捗更新処理完了: {fc2_id} - タスクが見つかりませんでした。") # デバッグログ追加
        self._status_updated_event.set() # 状態変更時にイベントをセット


    async def set_download_local_path(self, fc2_id: str, local_path: str):
        """ダウンロード完了後のローカルファイルパスを設定する"""
        async with self._lock:
            logging.debug(f"ローカルパス設定処理開始: {fc2_id} -> {local_path}") # デバッグログ追加
            if fc2_id in self.task_status:
                self.task_status[fc2_id]["local_path"] = local_path
                self.task_status[fc2_id]["last_updated"] = datetime.now().isoformat()
                logging.debug(f"ローカルパスを設定: {fc2_id} -> {local_path}") # ログ維持
                await self._save_status()
                logging.debug(f"ローカルパス設定処理完了: {fc2_id} - 状態を保存しました。") # デバッグログ追加
            else:
                logging.warning(f"ローカルパス設定対象のタスクが見つかりません: {fc2_id}") # ログ維持
                logging.debug(f"ローカルパス設定処理完了: {fc2_id} - タスクが見つかりませんでした。") # デバッグログ追加
        self._status_updated_event.set() # 状態変更時にイベントをセット


    async def get_all_status(self) -> Dict[str, Any]:
        """現在のすべてのタスクステータスとキュー情報を返す"""
        async with self._lock:
            logging.debug("全タスクステータス取得処理開始。") # デバッグログ追加
            # task_status のコピーを作成
            status_copy = {k: v.copy() for k, v in self.task_status.items()}

            # フロントエンドが必要とする 'progress' フィールドを追加
            for task_id, task_info in status_copy.items():
                status = task_info.get("status")
                if status == "downloading":
                    task_info["progress"] = task_info.get("download_progress", 0)
                # elif status == "uploading": # アップロード機能削除のため削除
                #     task_info["progress"] = task_info.get("upload_progress", 0)
                # elif status == "completed" or status == "skipped_upload": # skipped_upload も完了扱い # アップロード機能削除のため修正
                elif status == "completed": # ダウンロード完了のみ
                     # 完了の場合は100%
                     task_info["progress"] = 100.0
                else:
                    task_info["progress"] = 0.0 # その他の状態では0%

            logging.debug("全タスクステータス取得処理完了。") # デバッグログ追加
            return {
                "task_status": status_copy,
                "download_queue_count": len(self.download_queue),
                # "upload_queue_count": len(self.upload_queue), # アップロード機能削除のため削除
                "processed_count": len(self.processed_ids)
            }

    async def get_processed_ids(self) -> Set[str]:
        """処理済みのIDセットを返す"""
        async with self._lock:
            logging.debug("処理済みID取得処理開始/完了。") # デバッグログ追加
            return self.processed_ids.copy()

    async def get_task_status(self, fc2_id: str) -> Optional[Dict[str, Any]]:
        """指定されたタスクの現在のステータスを返す"""
        async with self._lock:
            logging.debug(f"タスクステータス取得処理開始: {fc2_id}") # デバッグログ追加
            task = self.task_status.get(fc2_id)
            if task:
                 task_copy = task.copy()
                 # フロントエンドが必要とする 'progress' フィールドを追加
                 status = task_copy.get("status")
                 if status == "downloading":
                     task_copy["progress"] = task_copy.get("download_progress", 0)
                 # elif status == "uploading": # アップロード機能削除のため削除
                 #     task_copy["progress"] = task_copy.get("upload_progress", 0)
                 # elif status == "completed" or status == "skipped_upload": # skipped_upload も完了扱い # アップロード機能削除のため修正
                 elif status == "completed": # ダウンロード完了のみ
                      task_copy["progress"] = 100.0
                 else:
                      task_copy["progress"] = 0.0
                 logging.debug(f"タスクステータス取得処理完了: {fc2_id} - タスク見つかりました。") # デバッグログ追加
                 return task_copy
            logging.debug(f"タスクステータス取得処理完了: {fc2_id} - タスク見つかりませんでした。") # デバッグログ追加
            return None


    async def request_stop(self):
        """停止リクエストフラグを立てる"""
        async with self._lock:
            logging.debug("停止リクエスト処理開始。") # デバッグログ追加
            self.stop_requested = True
            logging.info("停止リクエストを受け付けました。") # ログ維持
            logging.debug("停止リクエスト処理完了。") # デバッグログ追加
        self._status_updated_event.set() # 状態変更時にイベントをセット


    async def clear_stop_request(self):
        """停止リクエストフラグをクリアする"""
        async with self._lock:
            logging.debug("停止リクエストクリア処理開始。") # デバッグログ追加
            self.stop_requested = False
            logging.info("停止リクエストをクリアしました。") # ログ維持
            logging.debug("停止リクエストクリア処理完了。") # デバッグログ追加
        self._status_updated_event.set() # 状態変更時にイベントをセット


    async def resume_paused_tasks(self):
        """'paused' 状態のタスクを適切なキューに戻す"""
        async with self._lock:
            logging.info("中断されたタスクのレジューム処理を開始します。") # ログ維持
            logging.debug(f"レジューム処理開始時のtask_status: {json.dumps(self.task_status, indent=2)}") # デバッグログ維持
            logging.debug(f"レジューム処理開始時のdownload_queue: {list(self.download_queue)}") # デバッグログ維持
            # logging.debug(f"レジューム処理開始時のupload_queue: {list(self.upload_queue)}") # アップロード機能削除のため削除
            resumed_dl = 0
            # resumed_ul = 0 # アップロード機能削除のため削除
            ids_to_process = list(self.task_status.keys()) # イテレーション中の変更を避ける

            for fc2_id in ids_to_process:
                task = self.task_status.get(fc2_id)
                logging.debug(f"レジューム処理中タスク: {fc2_id} - Status: {task.get('status') if task else 'None'}, LocalPath: {task.get('local_path') if task else 'N/A'}") # デバッグログ追加
                if task and task.get("status") == "paused":
                    logging.debug(f"タスク {fc2_id} はpaused状態です。キューに戻すか判定。") # デバッグログ追加
                    # 中断されたタスクを適切なキューに戻す
                    # local_path があってもダウンロードキューに戻すように変更
                    logging.debug(f"タスク {fc2_id} をダウンロードキューに戻します。") # デバッグログ追加
                    # local_path があればアップロードキューに戻す # アップロード機能削除のため削除
                    # if task.get("local_path"): # アップロード機能削除のため削除
                    #     logging.debug(f"タスク {fc2_id} にlocal_pathがあります。アップロードキューに戻します。") # デバッグログ追加
                    #     # local_path があればアップロードキューに戻す # アップロード機能削除のため削除
                    #     if fc2_id not in self.upload_queue: # アップロード機能削除のため削除
                    #         self.upload_queue.appendleft(fc2_id) # 先頭に戻す # アップロード機能削除のため削除
                    #         task["status"] = "pending_upload" # アップロード機能削除のため削除
                    #         task["last_updated"] = datetime.now().isoformat() # アップロード機能削除のため削除
                    #         resumed_ul += 1 # アップロード機能削除のため削除
                    #         logging.info(f"中断されたアップロードタスクを再開キューに追加: {fc2_id}") # ログ維持 # アップロード機能削除のため削除
                    #         logging.debug(f"タスク {fc2_id} をアップロードキューに追加しました。") # デバッグログ追加 # アップロード機能削除のため削除
                    #     else: # アップロード機能削除のため削除
                    #         logging.debug(f"タスク {fc2_id} は既にアップロードキューに存在します。状態をpending_uploadに更新。") # デバッグログ維持 # アップロード機能削除のため削除
                    #         task["status"] = "pending_upload" # 状態だけ更新 # アップロード機能削除のため削除
                    #         task["last_updated"] = datetime.now().isoformat() # アップロード機能削除のため削除
                    # else: # アップロード機能削除のため削除
                    # local_path がなければダウンロードキューに戻す
                    if fc2_id not in self.download_queue:
                        self.download_queue.appendleft(fc2_id) # 先頭に戻す
                        task["status"] = "pending_download"
                        task["last_updated"] = datetime.now().isoformat()
                        resumed_dl += 1
                        logging.info(f"中断されたダウンロードタスクを再開キューに追加: {fc2_id}") # ログ維持
                        logging.debug(f"タスク {fc2_id} をダウンロードキューに追加しました。") # デバッグログ追加
                    else:
                        logging.debug(f"タスク {fc2_id} は既にダウンロードキューに存在します。状態をpending_downloadに更新。") # デバッグログ維持

            logging.info(f"中断されたタスクのレジューム処理が完了しました。ダウンロード: {resumed_dl} 件") # ログ維持
            logging.debug(f"レジューム処理後のtask_status: {json.dumps(self.task_status, indent=2)}") # デバッグログ維持
            logging.debug(f"レジューム処理後のdownload_queue: {list(self.download_queue)}") # デバッグログ維持
            # logging.debug(f"レジューム処理後のupload_queue: {list(self.upload_queue)}") # アップロード機能削除のため削除
            await self._save_status()
        self._status_updated_event.set() # 状態変更時にイベントをセット


    async def reset_failed_tasks(self):
        """'error', 'failed_download' 状態のタスクをリセットしてダウンロードキューに戻す"""
        async with self._lock:
            logging.debug("失敗タスクリセット処理開始。") # デバッグログ追加
            failed_ids_in_processed = [
                fc2_id for fc2_id in self.processed_ids
                if self.task_status.get(fc2_id, {}).get("status") in ["error", "failed_download"]
            ]
            for fc2_id in failed_ids_in_processed:
                self.processed_ids.remove(fc2_id)
                logging.debug(f"失敗タスク {fc2_id} をprocessed_idsから削除しました。") # デバッグログ追加

            reset_count = 0
            ids_to_process = list(self.task_status.keys()) # イテレーション中の変更を避ける

            for fc2_id in ids_to_process:
                task = self.task_status.get(fc2_id)
                if task and task.get("status") in ["error", "failed_download"]:
                    logging.info(f"失敗したタスク {fc2_id} をリセットし、ダウンロードキューに戻します。") # ログ維持
                    task["status"] = "pending_download"
                    task["download_progress"] = 0
                    task["error_message"] = None
                    task["last_updated"] = datetime.now().isoformat()
                    if fc2_id not in self.download_queue:
                        self.download_queue.appendleft(fc2_id) # キューの先頭に追加
                    reset_count += 1
                    logging.debug(f"タスク {fc2_id} をダウンロードキューに戻しました。") # デバッグログ追加
            logging.info(f"失敗したタスクのリセットが完了しました。{reset_count} 件のタスクをリセットしました。") # ログ維持
            await self._save_status()
        self._status_updated_event.set() # 状態変更時にイベントをセット


    async def mark_errors_as_completed(self):
        """
        本日までの 'error' 状態のタスクを 'completed' にマークする。
        これは、ユーザーの特定の要件に対応するためのものです。
        """
        async with self._lock:
            logging.info("エラー状態のタスクを完了としてマークする処理を開始します。")
            completed_count = 0
            today = datetime.now().date()

            ids_to_process = list(self.task_status.keys()) # イテレーション中の変更を避ける

            for fc2_id in ids_to_process:
                task = self.task_status.get(fc2_id)
                if task and task.get("status") in ["error", "failed_download"]:
                    last_updated_str = task.get("last_updated")
                    if last_updated_str:
                        try:
                            last_updated_date = datetime.fromisoformat(last_updated_str).date()
                            # 本日までのエラーを対象とする
                            if last_updated_date <= today:
                                logging.info(f"タスク {fc2_id} (エラー状態) を完了としてマークします。")
                                task["status"] = "completed"
                                task["download_progress"] = 100.0
                                task["error_message"] = None
                                task["last_updated"] = datetime.now().isoformat()
                                self.processed_ids.add(fc2_id) # 処理済みに追加
                                # ダウンロードキューから削除
                                if fc2_id in self.download_queue:
                                    self.download_queue.remove(fc2_id)
                                    logging.debug(f"タスク {fc2_id} をダウンロードキューから削除しました。")
                                completed_count += 1
                        except ValueError:
                            logging.warning(f"タスク {fc2_id} のlast_updated形式が無効です: {last_updated_str}")
                    else:
                        logging.warning(f"タスク {fc2_id} にlast_updated情報がありません。スキップします。")

            logging.info(f"エラー状態のタスクを完了としてマークする処理が完了しました。{completed_count} 件のタスクを完了としました。")
            await self._save_status()
        self._status_updated_event.set() # 状態変更時にイベントをセット


    async def mark_specific_tasks_as_completed(self, fc2_ids: List[str]):
        """
        指定されたFC2 IDのタスクを 'completed' にマークする。
        """
        async with self._lock:
            logging.info(f"指定されたタスクを完了としてマークする処理を開始します: {fc2_ids}")
            completed_count = 0

            for fc2_id in fc2_ids:
                task = self.task_status.get(fc2_id)
                if task:
                    if task.get("status") != "completed":
                        logging.info(f"タスク {fc2_id} を完了としてマークします。")
                        task["status"] = "completed"
                        task["download_progress"] = 100.0
                        task["error_message"] = None
                        task["last_updated"] = datetime.now().isoformat()
                        self.processed_ids.add(fc2_id) # 処理済みに追加
                        # ダウンロードキューから削除
                        if fc2_id in self.download_queue:
                            self.download_queue.remove(fc2_id)
                            logging.debug(f"タスク {fc2_id} をダウンロードキューから削除しました。")
                        completed_count += 1
                    else:
                        logging.info(f"タスク {fc2_id} は既に完了状態です。")
                else:
                    logging.warning(f"タスク {fc2_id} が見つかりませんでした。")

            logging.info(f"指定されたタスクを完了としてマークする処理が完了しました。{completed_count} 件のタスクを完了としました。")
            await self._save_status()
        self._status_updated_event.set() # 状態変更時にイベントをセット


    async def wait_for_status_update(self):
        """状態が更新されるまで待機する"""
        await self._status_updated_event.wait()
        self._status_updated_event.clear() # イベントをクリアして次の更新を待つ

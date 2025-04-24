import os
import requests
from flask import Flask, request, jsonify, send_from_directory
from datetime import datetime
from dotenv import load_dotenv
import json # ★json import を追加

load_dotenv() # .envファイルから環境変数を読み込む

app = Flask(__name__)

AFTERSHIP_API_KEY = os.getenv('AFTERSHIP_API_KEY')
AFTERSHIP_API_BASE_URL = 'https://api.aftership.com/v4'
ITEM_NAMES_FILE = 'item_names.json' # 品名保存用ファイル

# --- Item Name Persistence Functions ---
def load_item_names():
    """JSONファイルから品名データを読み込む"""
    try:
        if os.path.exists(ITEM_NAMES_FILE):
            with open(ITEM_NAMES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            return {}
    except (IOError, json.JSONDecodeError) as e:
        print(f"Error loading item names from {ITEM_NAMES_FILE}: {e}")
        return {} # エラー時は空の辞書を返す

def save_item_names():
    """品名データをJSONファイルに保存する"""
    try:
        with open(ITEM_NAMES_FILE, 'w', encoding='utf-8') as f:
            json.dump(tracking_item_names, f, ensure_ascii=False, indent=4)
    except IOError as e:
        print(f"Error saving item names to {ITEM_NAMES_FILE}: {e}")

# item_name を保持するためのローカルストア (ファイルから読み込み)
tracking_item_names = load_item_names()

# --- Helper Function ---
def format_aftership_response(aftership_data, tracking_number):
    """AfterShip APIのレスポンスをフロントエンド用の形式に変換する"""
    print(f"--- Formatting AfterShip data for {tracking_number} ---") # ★デバッグ出力追加
    # print(f"Input data: {json.dumps(aftership_data, indent=2, ensure_ascii=False)}") # ★デバッグ出力追加 (必要なら)

    tracking_info = aftership_data.get('data', {}).get('tracking', {})
    if not tracking_info:
        print(f"!!! No tracking_info found in AfterShip data for {tracking_number}") # ★デバッグ出力追加
        return None

    # ステータス判定 (AfterShipのtagを使用)
    tag = tracking_info.get('tag', 'Pending').lower()
    status_map = {
        'pending': ('未発送', 'status-unshipped'),
        'info_received': ('情報受信', 'status-shipped'), # Cainiaoの初期ステータスに近いもの
        'intransit': ('輸送中', 'status-intransit'),
        'outfordelivery': ('配達中', 'status-outfordelivery'),
        'delivered': ('配達済', 'status-delivered'),
        'attemptfail': ('配達失敗', 'status-unknown'), # 不明扱い
        'exception': ('例外発生', 'status-unknown'), # 不明扱い
    }
    status, status_class = status_map.get(tag, ('不明', 'status-unknown'))

    # 履歴 (checkpoints)
    history = []
    current_location = None
    last_updated = tracking_info.get('updated_at') or datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00") # Fallback

    for cp in sorted(tracking_info.get('checkpoints', []), key=lambda x: x.get('checkpoint_time', ''), reverse=True):
        ts_str = cp.get('checkpoint_time', '')
        # Aftershipのタイムスタンプ形式 (例: 2024-04-20T10:30:00+08:00) を整形
        try:
            # タイムゾーン情報を考慮してdatetimeオブジェクトに変換
            dt_obj = datetime.fromisoformat(ts_str)
            # 日本時間に変換 (例)
            # from dateutil import tz
            # dt_obj_jst = dt_obj.astimezone(tz.gettz('Asia/Tokyo'))
            # formatted_ts = dt_obj_jst.strftime("%Y-%m-%d %H:%M:%S")
            # 簡単のため、元の文字列をそのまま使うか、単純な形式に
            formatted_ts = dt_obj.strftime("%Y-%m-%d %H:%M") # シンプルな形式
        except (ValueError, TypeError):
             formatted_ts = ts_str # パース失敗時はそのまま

        history_item = {
            'timestamp': formatted_ts,
            'location': cp.get('location', '') or cp.get('city', '') or 'N/A',
            'description': cp.get('message', '')
        }
        history.append(history_item)

        # 最新のチェックポイントから位置情報を取得試行 (緯度経度は通常提供されない)
        if current_location is None and cp.get('coordinates'):
             # Aftershipのcoordinatesは [lng, lat] 形式の場合がある
             coords = cp.get('coordinates')
             if isinstance(coords, list) and len(coords) == 2:
                  current_location = {'lat': coords[1], 'lng': coords[0]}

    # 最新の更新日時を取得
    if history:
        last_updated = history[0]['timestamp'] # 最新の履歴の日時を使う

    # item_name をローカルストアから取得
    item_name = tracking_item_names.get(tracking_number, '')

    return {
        'tracking_number': tracking_number,
        'item_name': item_name,
        'status': status,
        'status_class': status_class,
        'last_updated': last_updated,
        'history': history,
        'current_location': current_location # 緯度経度がない場合はNone
    }


# --- Static Files ---
@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

# --- API Endpoints ---
@app.route('/api/track', methods=['POST'])
def add_tracking_item():
    """追跡番号をAfterShipに登録し、ローカルに品名を保存"""
    if not AFTERSHIP_API_KEY:
        return jsonify({'error': 'AfterShip API key is not configured'}), 500

    data = request.get_json()
    tracking_number = data.get('tracking_number')
    item_name = data.get('item_name')

    if not tracking_number:
        return jsonify({'error': 'Tracking number is required'}), 400

    # ローカルに品名を保存 (追跡番号をキーとする)
    if item_name:
        tracking_item_names[tracking_number] = item_name
        save_item_names() # 品名をファイルに保存

    headers = {
        'aftership-api-key': AFTERSHIP_API_KEY,
        'Content-Type': 'application/json'
    }
    payload = {
        "tracking": {
            "tracking_number": tracking_number,
            "slug": "cainiao" # Cainiaoを指定
            # 必要に応じて他のフィールドを追加 (titleなど)
        }
    }
    api_url = f"{AFTERSHIP_API_BASE_URL}/trackings"

    try:
        response = requests.post(api_url, headers=headers, json=payload)
        response.raise_for_status() # エラーがあれば例外を発生させる

        # 登録成功後、すぐに情報を取得して返す
        return get_tracking_info(tracking_number)

    except requests.exceptions.RequestException as e:
        print(f"Error registering tracking with AfterShip: {e}")
        # エラーレスポンスの内容を確認
        error_detail = "Failed to register tracking number with AfterShip."
        response_obj = getattr(e, 'response', None) # Get response object if available
        if response_obj is not None:
            try:
                error_response = response_obj.json()
                error_detail = error_response.get('meta', {}).get('message', error_detail)
            except:
                pass # JSONデコード失敗は無視

            # 409 Conflict (既に存在する) はエラーとしない場合もある
            if response_obj.status_code == 409:
                 print(f"Tracking number {tracking_number} already exists in AfterShip. Fetching info...")
                 return get_tracking_info(tracking_number) # 既存情報を取得して返す

            return jsonify({'error': error_detail}), response_obj.status_code
        else:
             return jsonify({'error': error_detail}), 500 # Generic server error if no response

    except Exception as e:
         print(f"Unexpected error in add_tracking_item: {e}")
         return jsonify({'error': 'An unexpected error occurred'}), 500


@app.route('/api/track/<string:tracking_number>', methods=['GET'])
def get_tracking_info(tracking_number):
    """AfterShip APIから追跡情報を取得"""
    if not AFTERSHIP_API_KEY:
        return jsonify({'error': 'AfterShip API key is not configured'}), 500

    headers = {
        'aftership-api-key': AFTERSHIP_API_KEY,
    }
    # Cainiaoのスラッグを指定
    api_url = f"{AFTERSHIP_API_BASE_URL}/trackings/cainiao/{tracking_number}"

    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status() # エラーチェック

        try:
            aftership_data = response.json()
            print(f"--- AfterShip Raw Response for {tracking_number} ---") # ★デバッグ出力追加
            # import json # Already imported at the top
            print(json.dumps(aftership_data, indent=2, ensure_ascii=False)) # ★デバッグ出力追加 (整形)
            print(f"--- End of Raw Response ---")
        except requests.exceptions.JSONDecodeError:
            print(f"!!! Failed to decode JSON from AfterShip for {tracking_number}. Response text: {response.text}")
            return jsonify({'error': 'Received invalid data from tracking provider'}), 502 # Bad Gateway

        formatted_data = format_aftership_response(aftership_data, tracking_number)
        print(f"--- Formatted Data for {tracking_number} ---") # ★デバッグ出力追加
        print(json.dumps(formatted_data, indent=2, ensure_ascii=False)) # ★デバッグ出力追加 (整形)
        print(f"--- End of Formatted Data ---")

        if formatted_data:
            print(f"取得成功 (AfterShip): {tracking_number}")
            return jsonify(formatted_data)
        else:
             print(f"AfterShip response format error or no tracking data for {tracking_number}")
             return jsonify({'error': 'Failed to parse tracking information from AfterShip'}), 500

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            print(f"Tracking number not found in AfterShip: {tracking_number}")
            # ローカルに品名情報があれば、それを含めてエラーを返すことも検討できる
            # item_name = tracking_item_names.get(tracking_number, '')
            return jsonify({'error': 'Tracking number not found'}), 404
        else:
            print(f"HTTP error fetching from AfterShip: {e}")
            error_detail = f"Failed to fetch tracking information from AfterShip (HTTP {e.response.status_code})."
            try:
                 error_response = e.response.json()
                 error_detail = error_response.get('meta', {}).get('message', error_detail)
            except:
                 pass
            return jsonify({'error': error_detail}), e.response.status_code
    except requests.exceptions.RequestException as e:
        print(f"Network error fetching from AfterShip: {e}")
        return jsonify({'error': 'Network error connecting to AfterShip'}), 503 # Service Unavailable
    except Exception as e:
         print(f"Unexpected error in get_tracking_info: {e}")
         return jsonify({'error': 'An unexpected error occurred'}), 500


if __name__ == '__main__':
    # ポート番号を指定する場合 (例: 5001)
    # port = int(os.environ.get('PORT', 5000))
    # app.run(host='0.0.0.0', port=port, debug=False) # デプロイ時は debug=False
    app.run(debug=True) # 開発時は debug=True
#!/bin/bash
# アプリケーションを起動します

# 現在のディレクトリをスクリプトのディレクトリに移動
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"

# 仮想環境が有効化されているか確認し、されていなければ有効化を試みる
VENV_PATH="/Users/walhalax/venv/tk-auto-dl"
if [ -z "$VIRTUAL_ENV" ]; then
    if [ -d "$VENV_PATH" ]; then
        echo "仮想環境を有効化します..."
        source "$VENV_PATH/bin/activate"
    else
        echo "指定された仮想環境 '$VENV_PATH' が見つかりません。仮想環境を作成し、依存関係をインストールしてください。"
        # 仮想環境がない場合でも実行を試みるか、エラーで終了するか選択。ここでは実行を試みる。
        exit 1
    fi
fi

# uvicornでFastAPIアプリケーションを起動
# --reload オプションは開発中に便利ですが、本番環境では不要な場合があります
echo "FastAPIアプリケーションを起動します..."
export PYTHONPATH=$PYTHONPATH:.
"$VENV_PATH/bin/uvicorn" src.web_app:app --reload --host 0.0.0.0 --port 8004 --app-dir "$SCRIPT_DIR"
sleep 3

echo "フロントエンドを開きます..."
open http://127.0.0.1:8004

# 仮想環境を無効化 (スクリプト終了時)
# deactivate # スクリプトが終了すると自動的に無効化されるため不要
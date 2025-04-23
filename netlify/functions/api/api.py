import serverless_wsgi
# app.py 内の Flask アプリケーションインスタンスをインポート
# ファイル名が app.py で、Flaskインスタンスが app という名前の場合
from app import app

def handler(event, context):
    """
    AWS Lambda / Netlify Functions handler.
    Wraps the Flask app using serverless-wsgi.
    """
    return serverless_wsgi.handle_request(app, event, context)
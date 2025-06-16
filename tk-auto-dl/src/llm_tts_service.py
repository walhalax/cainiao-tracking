import os
import httpx
import json
import logging
from typing import Optional, Dict, Any

# ロギング設定
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s')

# 環境変数からAPIキーとベースURLを取得
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"

# Eleven Labsの音声ID
# 和訳日本語話者男性(JP)
VOICE_ID_JP_MALE = "pNqE7q92y51111111111" # 仮のID
# 英訳英語話者男性(EN)
VOICE_ID_EN_MALE = "pNqE7q92y51111111112" # 仮のID
# スペイン訳スペイン語話者男性(ES)
VOICE_ID_ES_MALE = "pNqE7q92y51111111113" # 仮のID
# コメント言語返答女性
VOICE_ID_FEMALE_REPLY = "pNqE7q92y51111111114" # 仮のID

# LLMモデル名
LLM_MODEL_NAME = "mistralai/mistral-7b-instruct" # または gemini-pro など

async def call_llm(purpose: str, prompt: str, model: str = LLM_MODEL_NAME) -> Optional[str]:
    """
    OpenRouter.ai を介してLLMを呼び出す汎用関数。
    """
    if not OPENROUTER_API_KEY:
        logging.error("OPENROUTER_API_KEY が設定されていません。")
        return None

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}]
    }
    url = f"{OPENROUTER_BASE_URL}/chat/completions"

    logging.debug(f"[CALL_LLM] Purpose: {purpose}, Prompt: {prompt[:100]}...")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=60.0)
            response.raise_for_status()
            data = response.json()
            content = data['choices'][0]['message']['content'].strip()
            logging.info(f"[CALL_LLM] Success (Attempt 1): Raw LLM Response for {purpose} = '{content[:50]}...'")
            return content
    except httpx.RequestError as e:
        logging.error(f"[CALL_LLM] Request Error for {purpose}: {e}")
    except httpx.HTTPStatusError as e:
        logging.error(f"[CALL_LLM] HTTP Error for {purpose}: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        logging.error(f"[CALL_LLM] Unexpected Error for {purpose}: {e}", exc_info=True)
    return None

async def get_language(text: str) -> Optional[str]:
    """
    テキストの言語を判定する。
    """
    prompt = f"Detect the language of the following text. Respond ONLY with the BCP 47 language code (e.g., en, ja, es, fr, ko, zh-CN, pt, pt-BR, de, th, id, ar, vi):\n\n{text}"
    response = await call_llm("言語判定(get_language)", prompt)
    if response:
        # LLMの出力が '```ja```' のような形式の場合を考慮
        match = re.search(r'```(\w{2}(?:-\w{2})?)```', response)
        if match:
            return match.group(1)
        return response.strip().lower()
    return None

def clean_text_for_tts(text: str) -> str:
    """
    TTS用にテキストをクリーニングする。
    Markdownのコードブロックや余分な空白を削除。
    """
    logging.debug(f"[CLEAN_TTS] Input: '{text}'")
    # Markdownのコードブロックを削除
    cleaned_text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    # 余分な空白や改行を削除し、単一のスペースに置換
    cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()
    if not cleaned_text:
        logging.warning(f"clean_text_for_tts: Text became empty after cleaning. Original was: '{text}'")
    logging.debug(f"[CLEAN_TTS] Output: '{cleaned_text}'")
    return cleaned_text

async def generate_reply(comment_text: str, comment_lang: str) -> Optional[str]:
    """
    コメントに対して返信を生成する。
    """
    # プロンプトを調整して、女性AIアシスタントとして自然な返信を生成するように指示
    prompt = f"""You are a friendly female AI assistant responding to a comment in a livestream chat.
Generate a brief, natural-sounding response in Japanese to the following comment.
The original comment is in {comment_lang}.
Comment: "{comment_text}"
Response:"""
    reply_text = await call_llm(f"返信生成({comment_lang})", prompt)
    if reply_text:
        processed_reply_text = clean_text_for_tts(reply_text)
        if not processed_reply_text:
            logging.warning(f"generate_reply: 返信テキストがクリーニング後に空になりました。元の返信: '{reply_text}...'")
            return None
        logging.debug(f"generate_reply: Processed reply_text = '{processed_reply_text}'")
        return processed_reply_text
    logging.warning("generate_reply: 返信生成失敗。")
    return None

async def translate_text(text: str, target_lang: str) -> Optional[str]:
    """
    テキストを翻訳する。
    """
    prompt = f"Translate the following text into {target_lang}. Respond ONLY with the translated text:\n\n{text}"
    translated_text = await call_llm(f"翻訳({target_lang})", prompt)
    if translated_text:
        return clean_text_for_tts(translated_text)
    return None

async def text_to_speech(text: str, voice_id: str, filename: str) -> Optional[str]:
    """
    Eleven Labs APIを使用してテキストを音声に変換し、ファイルに保存する。
    """
    if not ELEVENLABS_API_KEY:
        logging.error("ELEVENLABS_API_KEY が設定されていません。")
        return None
    if not voice_id:
        logging.error("音声IDが指定されていません。")
        return None

    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2", # または eleven_japanese_v2 など
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75
        }
    }
    url = f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}"

    output_dir = "audio_output"
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, filename)

    logging.debug(f"[TTS] Generating speech for: '{text[:50]}...' with voice ID: {voice_id}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload, timeout=60.0)
            response.raise_for_status()

            with open(output_path, 'wb') as f:
                for chunk in response.iter_bytes():
                    f.write(chunk)
            logging.info(f"[TTS] Speech saved to {output_path}")
            return output_path
    except httpx.RequestError as e:
        logging.error(f"[TTS] Request Error: {e}")
    except httpx.HTTPStatusError as e:
        logging.error(f"[TTS] HTTP Error: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        logging.error(f"[TTS] Unexpected Error: {e}", exc_info=True)
    return None

async def process_comment_for_audio(comment_id: str, comment_text: str) -> Dict[str, Any]:
    """
    コメントを処理し、翻訳と返信の音声を生成する。
    """
    logging.info(f"--- Processing comment ID: {comment_id} ---")
    results = {"comment_id": comment_id, "original_comment": comment_text}

    # 1. コメントの言語判定
    comment_lang = await get_language(comment_text)
    results["comment_language"] = comment_lang
    logging.info(f"コメント言語判定結果: {comment_lang}")

    # 2. 翻訳と音声生成
    if comment_lang and comment_lang != "ja":
        # 日本語への翻訳
        translated_ja = await translate_text(comment_text, "ja")
        results["translated_ja"] = translated_ja
        if translated_ja:
            logging.info(f"日本語翻訳: {translated_ja}")
            audio_path_ja = await text_to_speech(translated_ja, VOICE_ID_JP_MALE, f"{comment_id}_ja.mp3")
            results["audio_path_ja"] = audio_path_ja
    
    if comment_lang and comment_lang != "en":
        # 英語への翻訳
        translated_en = await translate_text(comment_text, "en")
        results["translated_en"] = translated_en
        if translated_en:
            logging.info(f"英語翻訳: {translated_en}")
            audio_path_en = await text_to_speech(translated_en, VOICE_ID_EN_MALE, f"{comment_id}_en.mp3")
            results["audio_path_en"] = audio_path_en

    if comment_lang and comment_lang != "es":
        # スペイン語への翻訳
        translated_es = await translate_text(comment_text, "es")
        results["translated_es"] = translated_es
        if translated_es:
            logging.info(f"スペイン語翻訳: {translated_es}")
            audio_path_es = await text_to_speech(translated_es, VOICE_ID_ES_MALE, f"{comment_id}_es.mp3")
            results["audio_path_es"] = audio_path_es

    # 3. 返信生成と音声生成 (女性話者)
    reply_text = await generate_reply(comment_text, comment_lang if comment_lang else "unknown")
    results["reply_text"] = reply_text
    if reply_text:
        logging.info(f"返信テキスト: {reply_text}")
        audio_path_reply = await text_to_speech(reply_text, VOICE_ID_FEMALE_REPLY, f"{comment_id}_reply.mp3")
        results["audio_path_reply"] = audio_path_reply
    
    logging.info(f"処理完了: {comment_id}")
    return results

# テスト用のメイン関数 (必要に応じてコメント解除して実行)
# async def main():
#     # 環境変数を設定 (テスト用)
#     os.environ["OPENROUTER_API_KEY"] = "YOUR_OPENROUTER_API_KEY"
#     os.environ["ELEVENLABS_API_KEY"] = "YOUR_ELEVENLABS_API_KEY"
#     
#     test_comment_ja = "こんにちは、元気ですか？"
#     test_comment_en = "Hello, how are you?"
#     test_comment_es = "¿Hola, cómo estás?"
# 
#     print("\n--- 日本語コメントの処理 ---")
#     await process_comment_for_audio("test_ja_001", test_comment_ja)
# 
#     print("\n--- 英語コメントの処理 ---")
#     await process_comment_for_audio("test_en_001", test_comment_en)
# 
#     print("\n--- スペイン語コメントの処理 ---")
#     await process_comment_for_audio("test_es_001", test_comment_es)
# 
# if __name__ == "__main__":
#     asyncio.run(main())
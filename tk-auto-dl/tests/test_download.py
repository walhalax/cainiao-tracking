import pytest
from src.download_module import find_mp4_url
import logging

@pytest.mark.asyncio
async def test_find_mp4_url():
    url = 'https://tktube.com/ja/videos/314819/fc2-ppv-4670832-4-27-21/'
    result = await find_mp4_url(url)
    logging.info(f'取得したMP4 URL: {result}')
    assert result is not None, 'MP4 URLの抽出に失敗しました'
    assert result.endswith('.mp4'), '有効なMP4 URL形式ではありません'

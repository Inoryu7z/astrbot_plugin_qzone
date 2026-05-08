import asyncio
import base64
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote

import aiohttp

from astrbot.api import logger

BytesOrStr = str | bytes

_shared_session: aiohttp.ClientSession | None = None


async def _get_shared_session() -> aiohttp.ClientSession:
    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
    return _shared_session


async def download_file(url: str) -> bytes | None:
    """下载图片"""
    if url.startswith("file:///"):
        try:
            path = unquote(url[8:])
            return await asyncio.to_thread(Path(path).read_bytes)
        except Exception as e:
            logger.error(f"本地图片读取失败: {e}")
            return None
    try:
        session = await _get_shared_session()
        async with session.get(url) as response:
            if response.status != 200:
                logger.error(f"图片下载失败: HTTP {response.status}, url={url[:100]}")
                return None
            img_bytes = await response.read()
            return img_bytes
    except Exception as e:
        logger.error(f"图片下载失败: {e}")
        return None


async def close_shared_session() -> None:
    global _shared_session
    if _shared_session and not _shared_session.closed:
        await _shared_session.close()
    _shared_session = None


async def normalize_images(images: Sequence[BytesOrStr] | None) -> list[bytes]:
    """
    将 str/bytes 混合列表统一转成 bytes 列表：
    - str -> 下载后转 bytes（下载失败则忽略）
    - bytes -> 原样保留
    - None -> 空列表
    """
    if images is None:
        return []

    cleaned: list[bytes] = []
    for item in images:
        if isinstance(item, bytes):
            cleaned.append(item)
        elif isinstance(item, str):
            file = await download_file(item)
            if file is not None:
                cleaned.append(file)
        else:
            raise TypeError(f"image 必须是 str 或 bytes，收到 {type(item)}")
    return cleaned

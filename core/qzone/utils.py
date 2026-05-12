import asyncio
import base64
import io
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

import aiohttp

from astrbot.api import logger

BytesOrStr = str | bytes

_shared_session: aiohttp.ClientSession | None = None

_QZONE_SUPPORTED_FORMATS = {"JPEG", "PNG", "GIF", "BMP"}


def _convert_to_supported_format(img_bytes: bytes) -> bytes:
    """检测图片格式，如果不被QQ空间支持则转换为JPEG"""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(img_bytes))
        fmt = img.format

        if fmt in _QZONE_SUPPORTED_FORMATS:
            return img_bytes

        logger.info(f"图片格式 {fmt} 不被QQ空间支持，转换为JPEG")
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"图片格式转换失败，使用原图: {e}")
        return img_bytes


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
            path = unquote(urlparse(url).path)
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
            cleaned.append(_convert_to_supported_format(item))
        elif isinstance(item, str):
            file = await download_file(item)
            if file is not None:
                cleaned.append(_convert_to_supported_format(file))
        else:
            raise TypeError(f"image 必须是 str 或 bytes，收到 {type(item)}")
    return cleaned

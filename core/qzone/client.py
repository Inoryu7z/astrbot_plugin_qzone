# client.py

import asyncio
from typing import Any

import aiohttp

from astrbot.api import logger

from ..config import PluginConfig
from .constants import (
    HTTP_STATUS_FORBIDDEN,
    HTTP_STATUS_UNAUTHORIZED,
    QZONE_CODE_LOGIN_EXPIRED,
    QZONE_CODE_UNKNOWN,
    QZONE_INTERNAL_HTTP_STATUS_KEY,
    QZONE_INTERNAL_META_KEY,
    QZONE_MSG_PERMISSION_DENIED,
)
from .parser import QzoneParser
from .session import QzoneSession

RETRY_DELAY_AFTER_LOGIN = 2
MAX_LOGIN_RETRY_IN_REQUEST = 2


class QzoneHttpClient:
    def __init__(self, session: QzoneSession, config: PluginConfig):
        self.cfg = config
        self.session = session
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.cfg.timeout)
        )

    async def close(self):
        await self._session.close()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        retry: int = 0,
    ) -> dict[str, Any]:
        ctx = await self.session.get_ctx()
        merged_headers = dict(ctx.headers())
        if headers:
            merged_headers.update(headers)
        req_timeout = aiohttp.ClientTimeout(total=timeout) if timeout is not None else None
        async with self._session.request(
            method,
            url,
            params=params,
            data=data,
            headers=merged_headers,
            cookies=ctx.cookies(),
            timeout=req_timeout,
        ) as resp:
            text = await resp.text()

        parsed = QzoneParser.parse_response(text)
        meta = parsed.get(QZONE_INTERNAL_META_KEY)
        if not isinstance(meta, dict):
            meta = {}
            parsed[QZONE_INTERNAL_META_KEY] = meta
        meta[QZONE_INTERNAL_HTTP_STATUS_KEY] = resp.status

        if not text:
            logger.warning(f"[QQ空间] API 返回空响应体 (HTTP {resp.status}, URL: {url})")
            if retry < MAX_LOGIN_RETRY_IN_REQUEST:
                logger.info(f"[QQ空间] 空响应重试 ({retry + 1}/{MAX_LOGIN_RETRY_IN_REQUEST})，刷新登录态...")
                await self.session.refresh_login()
                await asyncio.sleep(RETRY_DELAY_AFTER_LOGIN)
                return await self.request(
                    method, url, params=params, data=data, headers=headers,
                    timeout=timeout, retry=retry + 1,
                )

        if _is_login_expired(resp.status, parsed):
            if retry >= MAX_LOGIN_RETRY_IN_REQUEST:
                raise RuntimeError(
                    f"登录失效，已在请求层重试 {retry} 次 (HTTP {resp.status})"
                )

            logger.warning(f"[QQ空间] 登录态失效 (HTTP {resp.status})，触发 Cookie 刷新流程")
            await self.session.refresh_login()
            await asyncio.sleep(RETRY_DELAY_AFTER_LOGIN)
            return await self.request(
                method, url, params=params, data=data, headers=headers,
                timeout=timeout, retry=retry + 1,
            )

        if resp.status == HTTP_STATUS_FORBIDDEN and parsed.get("code") in (
            QZONE_CODE_UNKNOWN,
            None,
        ):
            parsed["code"] = resp.status
            parsed["message"] = QZONE_MSG_PERMISSION_DENIED

        return parsed


def _is_login_expired(http_status: int, parsed: dict) -> bool:
    if http_status == HTTP_STATUS_UNAUTHORIZED:
        return True
    if http_status in (403, 500, 502, 503):
        code = parsed.get("code")
        if code == QZONE_CODE_LOGIN_EXPIRED:
            return True
        if isinstance(code, str) and str(code) == str(QZONE_CODE_LOGIN_EXPIRED):
            return True
    return parsed.get("code") == QZONE_CODE_LOGIN_EXPIRED

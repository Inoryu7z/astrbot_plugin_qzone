# session.py

import asyncio
from http.cookies import SimpleCookie
from typing import Any

from astrbot.api import logger

from ..config import PluginConfig
from .model import QzoneContext


class QzoneSession:
    DOMAIN = "user.qzone.qq.com"
    VALIDATION_URL = (
        "https://h5.qzone.qq.com/proxy/domain/"
        "g.qzone.qq.com/cgi-bin/friendshow/cgi_get_visitor_more"
    )
    REFRESH_RETRIES = 3
    REFRESH_DELAYS = (2, 5, 8)

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._ctx: QzoneContext | None = None
        self._lock = asyncio.Lock()
        self._last_cookies_hash: str = ""

    async def get_ctx(self) -> QzoneContext:
        async with self._lock:
            if not self._ctx:
                self._ctx = await self._login_internal()
            return self._ctx

    async def get_uin(self) -> int:
        ctx = await self.get_ctx()
        return ctx.uin

    async def get_nickname(self) -> str:
        ctx = await self.get_ctx()
        uin = str(ctx.uin)
        if not self.cfg.client:
            return uin
        try:
            info = await self.cfg.client.get_login_info()
            return info.get("nickname") or uin
        except Exception:
            return uin

    async def invalidate(self) -> None:
        async with self._lock:
            self._ctx = None

    async def login(self, cookies_str: str | None = None) -> QzoneContext:
        async with self._lock:
            self._ctx = await self._login_internal(cookies_str)
            return self._ctx

    async def refresh_login(self, force_new: bool = True) -> QzoneContext:
        async with self._lock:
            if force_new:
                self._ctx = None
            return await self._refresh_login_internal()

    async def _refresh_login_internal(self) -> QzoneContext:
        last_error: Exception | None = None
        for i in range(self.REFRESH_RETRIES):
            delay = self.REFRESH_DELAYS[min(i, len(self.REFRESH_DELAYS) - 1)]
            if i > 0:
                logger.info(f"[QQ空间] 等待 {delay} 秒后进行第 {i + 1}/{self.REFRESH_RETRIES} 次 Cookie 刷新...")
                await asyncio.sleep(delay)

            try:
                ctx = await self._login_internal()
                if not ctx.p_skey:
                    logger.warning(f"[QQ空间] Cookie 刷新结果缺少 p_skey (第 {i + 1} 次)")
                    last_error = RuntimeError("Cookie 中缺少 p_skey")
                    continue

                validated = await self._validate_ctx(ctx)
                if validated:
                    self._ctx = ctx
                    logger.info(f"[QQ空间] Cookie 刷新成功 (uin={ctx.uin})")
                    return ctx

                logger.warning(f"[QQ空间] Cookie 验证未通过 (第 {i + 1} 次)")
                last_error = RuntimeError("Cookie 验证未通过")
            except Exception as e:
                last_error = e
                logger.warning(f"[QQ空间] Cookie 刷新异常 (第 {i + 1} 次): {e}")

        msg = f"Cookie 刷新失败（已重试 {self.REFRESH_RETRIES} 次）"
        if last_error:
            msg = f"{msg}: {last_error}"
        raise RuntimeError(msg)

    async def _login_internal(self, cookies_str: str | None = None) -> QzoneContext:
        logger.info("[QQ空间] 正在获取登录凭证...")

        if not cookies_str:
            cookies_str, source = await self._acquire_cookies()
        else:
            source = "手动传入"
            logger.debug(f"[QQ空间] 使用 {source} cookies (长度: {len(cookies_str)})")

        c = {k: v.value for k, v in SimpleCookie(cookies_str).items()}
        uin_raw = c.get("uin", "0")
        uin = int(uin_raw[1:] if uin_raw.startswith("o") else uin_raw)
        if not uin:
            raise RuntimeError("Cookie 中缺少合法 uin")

        skey = c.get("skey", "")
        p_skey = c.get("p_skey", "")

        if not p_skey:
            logger.warning(f"[QQ空间] {source} cookies 中缺少 p_skey（发布说说需要此字段）")
        if not skey:
            logger.warning(f"[QQ空间] {source} cookies 中缺少 skey")

        ctx = QzoneContext(uin=uin, skey=skey, p_skey=p_skey)
        logger.info(
            f"[QQ空间] 凭证就绪 (uin={uin}, source={source}, "
            f"p_skey={'有' if p_skey else '⚠无'}, skey={'有' if skey else '⚠无'})"
        )
        return ctx

    async def _acquire_cookies(self) -> tuple[str, str]:
        if self.cfg.client:
            try:
                result = await self.cfg.client.get_cookies(domain=self.DOMAIN)
                if isinstance(result, dict):
                    raw = result.get("cookies", "")
                elif isinstance(result, str):
                    raw = result
                else:
                    raw = ""
                if raw:
                    self.cfg.update_cookies(raw)
                    return raw, "CQHttp"
            except Exception as e:
                logger.warning(f"[QQ空间] CQHttp get_cookies 失败: {e}")

        fallback = self.cfg.cookies_str
        if fallback:
            return fallback, "配置cookies_str"

        raise RuntimeError("无法获取 Cookie：CQHttp 和配置 cookies_str 均不可用")

    async def _validate_ctx(self, ctx: QzoneContext) -> bool:
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.VALIDATION_URL,
                    params={
                        "uin": ctx.uin,
                        "mask": 7,
                        "g_tk": ctx.gtk2,
                        "page": 1,
                        "fupdate": 1,
                        "clear": 1,
                    },
                    cookies=ctx.cookies(),
                    headers=ctx.headers(),
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    text = await resp.text()
                    ok = resp.status == 200 and len(text) > 0
                    if ok:
                        logger.debug(f"[QQ空间] Cookie 验证通过 (HTTP {resp.status}, 响应 {len(text)} 字节)")
                    else:
                        logger.warning(
                            f"[QQ空间] Cookie 验证失败: HTTP {resp.status}, 响应 {len(text)} 字节"
                        )
                    return ok
        except Exception as e:
            logger.warning(f"[QQ空间] Cookie 验证网络异常: {e}")
            return False

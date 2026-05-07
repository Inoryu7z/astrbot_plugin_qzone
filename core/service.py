# service.py

import asyncio
import time
from typing import Any

from astrbot.api import logger

from .db import PostDB
from .llm_action import LLMAction
from .model import Comment, Post
from .qzone import QzoneAPI, QzoneParser, QzoneSession
from .qzone.constants import (
    HTTP_STATUS_FORBIDDEN,
    QZONE_CODE_LOGIN_EXPIRED,
    QZONE_CODE_PERMISSION_DENIED,
    QZONE_CODE_PERMISSION_DENIED_LEGACY,
    QZONE_CODE_UNKNOWN,
    QZONE_INTERNAL_HTTP_STATUS_KEY,
    QZONE_INTERNAL_META_KEY,
    QZONE_MSG_EMPTY_RESPONSE,
    QZONE_MSG_INVALID_RESPONSE,
    QZONE_MSG_JSON_PARSE_ERROR,
    QZONE_MSG_NON_OBJECT_RESPONSE,
    QZONE_MSG_PERMISSION_DENIED,
)

RETRY_DELAYS = (10, 30, 60)


async def _retry(func, *args, name: str = "", **kwargs):
    last_error = None
    for i, delay in enumerate(RETRY_DELAYS):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if i < len(RETRY_DELAYS) - 1:
                logger.warning(f"{name} 失败，{delay}秒后重试 ({i + 1}/{len(RETRY_DELAYS)}): {e}")
                await asyncio.sleep(delay)
    raise last_error


async def _retry_with_refresh(func, session: QzoneSession, *args, name: str = "", **kwargs):
    last_error = None
    for i, delay in enumerate(RETRY_DELAYS):
        try:
            result = await func(*args, **kwargs)
            if hasattr(result, 'ok') and not result.ok:
                code = getattr(result, 'code', None)
                message = getattr(result, 'message', None) or ''
                raise RuntimeError(f"API 返回失败 (code={code}, message={message})")
            return result
        except Exception as e:
            last_error = e
            if i < len(RETRY_DELAYS) - 1:
                logger.warning(
                    f"{name} 失败，{delay}秒后刷新 Cookie 并重试 ({i + 1}/{len(RETRY_DELAYS)}): {e}"
                )
                await asyncio.sleep(delay)
                await session.invalidate()
    raise last_error


def _build_publish_error(resp: Any) -> str:
    code = getattr(resp, 'code', None) if hasattr(resp, 'code') else resp.get('code') if isinstance(resp, dict) else None
    message = getattr(resp, 'message', None) if hasattr(resp, 'message') else resp.get('message') if isinstance(resp, dict) else None
    http_status = None
    raw = getattr(resp, 'raw', {}) if hasattr(resp, 'raw') else resp.get('raw', {}) if isinstance(resp, dict) else {}
    if isinstance(raw, dict):
        meta = raw.get(QZONE_INTERNAL_META_KEY, {})
        if isinstance(meta, dict):
            http_status = meta.get(QZONE_INTERNAL_HTTP_STATUS_KEY)

    parts = []
    if code is not None and code != 0:
        parts.append(f"code={code}")
    if http_status is not None:
        parts.append(f"HTTP {http_status}")
    if message and message != QZONE_MSG_EMPTY_RESPONSE:
        parts.append(str(message))

    if not parts:
        return "发布说说失败：服务器返回空响应，请检查登录态"

    return f"发布说说失败：{', '.join(parts)}"


class PostService:

    def __init__(
        self,
        qzone: QzoneAPI,
        session: QzoneSession,
        db: PostDB,
        llm: LLMAction,
    ):
        self.qzone = qzone
        self.session = session
        self.db = db
        self.llm = llm

    async def query_feeds(
        self,
        *,
        target_id: str | None = None,
        pos: int = 0,
        num: int = 1,
        with_detail: bool = False,
        no_self: bool = False,
        no_commented: bool = False,
    ) -> list[Post]:
        if target_id:
            resp = await self.qzone.get_feeds(target_id, pos=pos, num=num)
            if not resp.ok:
                raise RuntimeError(self._map_feed_error(resp, target_id=target_id))
            msglist = resp.data.get("msglist") or []
            if not msglist:
                raise RuntimeError(f"QQ {target_id} 暂无可见说说")
            posts: list[Post] = QzoneParser.parse_feeds(msglist)

        else:
            resp = await self.qzone.get_recent_feeds()
            if not resp.ok:
                raise RuntimeError(self._map_feed_error(resp))
            posts: list[Post] = QzoneParser.parse_recent_feeds(resp.data)[
                pos : pos + num
            ]
            if not posts:
                raise RuntimeError("动态流暂无可见说说")

        if no_self:
            uin = await self.session.get_uin()
            posts = [p for p in posts if p.uin != uin]

        if with_detail:
            posts = await self._fill_post_detail(posts)
            if not posts:
                raise RuntimeError("获取详情后无有效说说")

        if no_commented:
            posts = await self._filter_not_commented(posts)

        for post in posts:
            await self.db.save(post)

        return posts

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(k in text for k in keywords)

    def _map_feed_error(self, resp, *, target_id: str | None = None) -> str:
        message = str(resp.message or "").strip()
        lower_message = message.lower()
        code = resp.code
        http_status = self._extract_http_status(resp.raw)

        permission_keywords = (
            "无权限",
            "权限",
            "私密",
            "不可见",
            "拒绝访问",
            "受限",
            "forbidden",
            QZONE_MSG_PERMISSION_DENIED,
            "access denied",
        )
        login_keywords = ("登录", "失效", "skey", "g_tk", "cookie", "expired")

        if code == QZONE_CODE_LOGIN_EXPIRED or self._contains_any(
            lower_message, login_keywords
        ):
            return "登录状态失效，请重新登录后重试"

        if (
            code in (QZONE_CODE_PERMISSION_DENIED, QZONE_CODE_PERMISSION_DENIED_LEGACY)
            or http_status == HTTP_STATUS_FORBIDDEN
            or self._contains_any(lower_message, permission_keywords)
        ):
            if target_id:
                return f"无权限查看 QQ {target_id} 的说说"
            return "无权限访问动态流"

        if code == QZONE_CODE_UNKNOWN and message == QZONE_MSG_EMPTY_RESPONSE:
            if target_id:
                return f"无权限查看 QQ {target_id} 的说说（接口返回空响应）"
            return "动态接口返回空响应，请稍后重试"

        if code == QZONE_CODE_UNKNOWN and message in (
            QZONE_MSG_INVALID_RESPONSE,
            QZONE_MSG_JSON_PARSE_ERROR,
            QZONE_MSG_NON_OBJECT_RESPONSE,
        ):
            return "接口响应格式异常，请稍后重试"

        if message:
            return f"查询说说失败：{message}"
        return f"查询说说失败：code={code}"

    @staticmethod
    def _extract_http_status(raw: dict[str, Any]) -> int | None:
        meta = raw.get(QZONE_INTERNAL_META_KEY)
        if not isinstance(meta, dict):
            return None
        status = meta.get(QZONE_INTERNAL_HTTP_STATUS_KEY)
        return status if isinstance(status, int) else None

    @staticmethod
    def _has_comment_from_uin(post: Post, uin: int) -> bool:
        return any(comment.uin == uin for comment in post.comments)

    async def _has_saved_self_comment(self, post: Post, uin: int) -> bool:
        if not post.tid:
            return False
        saved_post = await self.db.get(post.tid, key="tid")
        return bool(saved_post and self._has_comment_from_uin(saved_post, uin))

    async def _fill_post_detail(self, posts: list[Post]) -> list[Post]:
        result: list[Post] = []

        for post in posts:
            resp = await self.qzone.get_detail(post)
            if not resp.ok or not resp.data:
                logger.warning(f"获取详情失败：{resp.data}")
                continue

            parsed = QzoneParser.parse_feeds([resp.data])
            if not parsed:
                logger.warning(f"解析详情失败：{resp.data}")
                continue

            result.append(parsed[0])

        return result

    async def _filter_not_commented(self, posts: list[Post]) -> list[Post]:
        result: list[Post] = []
        uin = await self.session.get_uin()

        for post in posts:
            if self._has_comment_from_uin(post, uin):
                continue
            if await self._has_saved_self_comment(post, uin):
                continue

            if not post.comments:
                resp = await self.qzone.get_detail(post)
                if not resp.ok or not resp.data:
                    continue
                parsed = QzoneParser.parse_feeds([resp.data])
                if not parsed:
                    continue
                post = parsed[0]

            if self._has_comment_from_uin(post, uin):
                continue

            result.append(post)

        return result

    async def view_visitor(self) -> str:
        resp = await self.qzone.get_visitor()
        if not resp.ok:
            raise RuntimeError(f"获取访客异常：{resp.data}")
        if not resp.data:
            raise RuntimeError("无访客记录")
        return QzoneParser.parse_visitors(resp.data)

    async def like_posts(self, post: Post):
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        await _retry(self.qzone.like, post, name="点赞")
        logger.info(f"已点赞 → {post.name}")

    async def comment_posts(self, post: Post):
        if not post.tid:
            raise ValueError("帖子 tid 为空")

        content = await self.llm.generate_comment(post)
        if not content:
            raise ValueError("生成评论内容为空")

        await _retry(self.qzone.comment, post, content, name="评论")

        uin = await self.session.get_uin()
        name = await self.session.get_nickname()
        post.comments.append(
            Comment(
                uin=uin,
                nickname=name,
                content=content,
                create_time=int(time.time()),
                tid=0,
                parent_tid=None,
            )
        )
        await self.db.save(post)
        logger.info(f"评论 → {post.name}")

    async def reply_comment(self, post: Post, index: int):

        if not post.tid:
            raise ValueError("帖子 tid 为空")

        uin = await self.session.get_uin()

        other_comments = [c for c in post.comments if c.uin != uin]
        n = len(other_comments)

        if n == 0:
            raise ValueError("没有可回复的评论")

        if not (-n <= index < n):
            raise ValueError(f"索引越界, 当前仅有 {n} 条可回复评论")

        comment = other_comments[index]

        content = await self.llm.generate_reply(post, comment)
        if not content:
            raise ValueError("生成回复内容为空")

        resp = await _retry(self.qzone.reply, post, comment, content, name="回复评论")
        if not resp.ok:
            raise RuntimeError(resp.message)

        name = await self.session.get_nickname()
        post.comments.append(
            Comment(
                uin=uin,
                nickname=name,
                content=content,
                create_time=int(time.time()),
                parent_tid=comment.tid,
            )
        )
        await self.db.save(post)

    async def publish_post(
        self,
        *,
        post: Post | None = None,
        text: str | None = None,
        images: list | None = None,
    ) -> Post:

        if post is None and not text and not images:
            raise ValueError("post、text、images 不能同时为空")

        if post is None:
            uin = await self.session.get_uin()
            name = await self.session.get_nickname()
            post = Post(
                uin=uin,
                name=name,
                text=text or "",
                images=images or [],
            )

        await _retry(self.qzone.get_visitor, name="登录态预检")

        resp = await _retry_with_refresh(
            self.qzone.publish, self.session, post, name="发布说说"
        )
        if not resp.ok:
            raise RuntimeError(_build_publish_error(resp))

        post.tid = resp.data.get("tid")
        post.status = "approved"
        post.create_time = resp.data.get("now", post.create_time)

        await self.db.save(post)
        return post

    async def delete_post(self, post: Post):
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        await _retry(self.qzone.delete, post.tid, name="删除说说")
        if post.id:
            await self.db.delete(post.id)

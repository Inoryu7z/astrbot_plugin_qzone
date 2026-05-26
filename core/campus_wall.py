from astrbot.core.platform.astr_message_event import AstrMessageEvent

from astrbot.api import logger

from typing import Any

from .config import PluginConfig
from .db import PostDB
from .model import Post
from .sender import Sender
from .utils import get_image_urls


def _safe_sender_name(event: AstrMessageEvent) -> str:
    for getter in ("get_sender_name", "get_sender_nickname"):
        method = getattr(event, getter, None)
        if callable(method):
            try:
                value = method()
            except Exception:
                value = None
            if value:
                return str(value)
    message_obj = getattr(event, "message_obj", None)
    sender = getattr(message_obj, "sender", None)
    for attr in ("nickname", "card", "name"):
        value = getattr(sender, attr, None)
        if value:
            return str(value)
    return str(getattr(event, "get_sender_id", lambda: "0")() or "0")


def _safe_sender_id(event: AstrMessageEvent) -> int:
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        try:
            value = getter()
            if value is not None:
                return int(value)
        except Exception:
            pass
    message_obj = getattr(event, "message_obj", None)
    sender = getattr(message_obj, "sender", None)
    return int(getattr(sender, "user_id", 0) or 0)


def _safe_group_id(event: AstrMessageEvent) -> int:
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            value = getter()
            if value:
                return int(value)
        except Exception:
            pass
    return 0


def _safe_self_id(event: AstrMessageEvent) -> str:
    getter = getattr(event, "get_self_id", None)
    if callable(getter):
        try:
            value = getter()
            if value is not None:
                return str(value)
        except Exception:
            pass
    return "0"


def _safe_bot(event: AstrMessageEvent) -> Any:
    return getattr(event, "bot", None)


class CampusWall:
    def __init__(
        self,
        config: PluginConfig,
        controller: Any,
        db: PostDB,
        sender: Sender,
    ):
        self.cfg = config
        self.controller = controller
        self.db = db
        self.sender = sender

    async def contribute(self, event: AstrMessageEvent, anon: bool = False):
        sender_name = _safe_sender_name(event)
        raw_text = event.message_str.partition(" ")[2]
        text = f"{raw_text}"
        images = await get_image_urls(event)
        post = Post(
            uin=_safe_sender_id(event),
            name=sender_name,
            gin=_safe_group_id(event),
            text=text,
            images=images,
            anon=anon,
            status="pending",
        )
        await self.db.save(post)

        if not self.cfg.silent_approve:
            bot = _safe_bot(event) or self.cfg.client
            await self.sender.send_admin_post(
                post,
                client=bot,
                message=f"收到新投稿#{post.id}",
            )
        yield event.plain_result("投稿已提交，等待审核。")

    async def delete(self, event: AstrMessageEvent):
        args = event.message_str.split(" ")
        post_id = args[1] if len(args) >= 2 else -1
        reason = event.message_str.removeprefix(f"撤稿 {post_id}").strip()
        post = await self.db.get(post_id)
        if not post or not post.id:
            yield event.plain_result(f"稿件#{post_id}不存在")
            return
        if post.uin != _safe_sender_id(event):
            yield event.plain_result("你只能撤回自己的稿件")
            return
        await self.db.delete(post.id)
        msg = f"稿件#{post.id}已撤回"
        if reason:
            msg += f"\n理由：{reason}"
        bot = _safe_bot(event) or self.cfg.client
        await self.sender.send_admin_post(post, client=bot, message=msg)
        yield event.plain_result(msg)

    async def view(self, event: AstrMessageEvent):
        args = event.message_str.split(" ")[1:] or ["-1"]
        for post_id in args:
            if not post_id.isdigit():
                continue
            post = await self.db.get(post_id)
            if not post:
                yield event.plain_result(f"稿件#{post_id}不存在")
                continue
            await self.sender.send_post(event, post)

    async def approve(self, event: AstrMessageEvent):
        args = event.message_str.split(" ")
        post_id = args[1] if len(args) >= 2 else -1
        post = await self.db.get(post_id)
        if not post:
            yield event.plain_result(f"稿件#{post_id}不存在")
            return

        if post.status == "approved":
            yield event.plain_result(f"稿件#{post.id}已通过，请勿重复通过")
            return
        if self.cfg.show_name:
            post.text = f"【来自 {post.show_name} 的投稿】\n\n{post.text}"

        try:
            images = [img for img in post.images if isinstance(img, str)]
            media = [{"kind": "image", "source": url} for url in images]
            result = await self.controller.publish_post(content=post.text, media=media)
            if not result.get("ok"):
                raise RuntimeError(result.get("message", "发布失败"))
            post.tid = str(result.get("fid", ""))
        except Exception as e:
            logger.error(f"发布投稿失败: {e}")
            yield event.plain_result("发布投稿失败，请稍后重试")
            return

        if not self.cfg.silent_approve:
            await self.sender.send_post(event, post, message=f"已发布说说#{post.id}")
            if (
                str(post.uin) != _safe_self_id(event)
                and str(post.gin) != str(_safe_group_id(event))
            ):
                bot = _safe_bot(event) or self.cfg.client
                await self.sender.send_user_post(
                    post,
                    client=bot,
                    message=f"您的投稿#{post.id}已通过",
                )
        yield event.plain_result(f"已通过稿件#{post.id}")

    async def reject(self, event: AstrMessageEvent):
        args = event.message_str.split(" ")
        post_id = args[1] if len(args) >= 2 else -1
        reason = event.message_str.removeprefix(f"拒绝稿件 {post_id}").strip()
        post = await self.db.get(post_id)
        if not post:
            yield event.plain_result(f"稿件#{post_id}不存在")
            return

        if post.status == "rejected":
            yield event.plain_result(f"稿件#{post.id}已拒绝，请勿重复拒绝")
            return

        if post.status == "approved":
            yield event.plain_result(f"稿件#{post.id}已发布，无法拒绝")
            return

        reason = event.message_str.removeprefix(f"拒绝稿件 {post.id}").strip()

        post.status = "rejected"
        if reason:
            post.extra_text = reason
        await self.db.save(post)

        admin_msg = f"已拒绝稿件#{post.id}"
        if reason:
            admin_msg += f"\n理由：{reason}"

        if not self.cfg.silent_approve:
            if (
                str(post.uin) != _safe_self_id(event)
                and str(post.gin) != str(_safe_group_id(event))
            ):
                user_msg = f"您的投稿#{post.id}未通过"
                if reason:
                    user_msg += f"\n理由：{reason}"
                bot = _safe_bot(event) or self.cfg.client
                await self.sender.send_user_post(
                    post, client=bot, message=user_msg
                )
        yield event.plain_result(admin_msg)

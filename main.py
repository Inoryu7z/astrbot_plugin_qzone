"""AstrBot entry point for the QQ 空间 plugin (daemon-based architecture)."""

# ruff: noqa: E402
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import random
import re
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_ROOT = Path(__file__).resolve().parent
PLUGIN_DATA_NAME = "astrbot_plugin_qzone_Inoryu7z"
REQUIRED_QZONE_BRIDGE_API_VERSION = 2026052305
LEGACY_MIGRATION_FILES = ("state.json", "drafts.json", "posts.json")
LEGACY_MIGRATION_SENTINEL = ".legacy-qzone-migration.json"
LEGACY_MIGRATION_LOCK = ".legacy-qzone-migration.lock"
AUTO_BIND_RETRY_ATTEMPTS = 3
AUTO_BIND_RETRY_DELAY_SECONDS = 1.0
UNKNOWN_POST_TIME_TEXT = "未知时间"

SENSITIVE_LOG_KEYS = {
    "cookie",
    "cookies",
    "p_skey",
    "skey",
    "pt4_token",
    "pt_key",
    "qzonetoken",
    "secret",
    "token",
}
SENSITIVE_URL_QUERY_KEYS = {"g_tk", "gtk", "p_skey", "skey", "pt4_token", "pt_key", "qzonetoken", "token", "secret"}
LLM_INTERNAL_KEYS = SENSITIVE_LOG_KEYS | {"raw", "cursor", "fid", "curkey", "unikey", "busi_param"}
LLM_REPLY_FORBIDDEN_TERMS = (
    "Result:",
    "result:",
    "[TOOL_",
    "TOOL_",
    "qzone_",
    "qzone_like_post",
    "qzone_comment_post",
    "qzone_publish_post",
    "qzone_view_post",
    "qzone_delete_post",
    "JSON",
    "json",
    "Markdown",
    "markdown",
    "字段",
    "fid",
    "hostuin",
    "status_code",
    "diagnostic",
    "API",
    "api",
    "工具",
    "系统",
    "后台",
    "参数",
    "指令",
    "命令",
    "内部",
    "错误代码",
    "状态码",
    "生成",
    "绘制",
    "绘图",
    "渲染",
    "处理完成",
    "任务完成",
    "已发送",
)


def _redact_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except Exception:
        return value
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return value
    query = []
    changed = False
    for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in SENSITIVE_URL_QUERY_KEYS or "token" in lowered or "skey" in lowered:
            query.append((key, "***"))
            changed = True
        else:
            query.append((key, item_value))
    if not changed:
        return value
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _redact_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in SENSITIVE_LOG_KEYS or "cookie" in lowered or "skey" in lowered or "secret" in lowered:
                redacted[key_text] = "***"
            else:
                redacted[key_text] = _redact_for_log(item)
        return redacted
    if isinstance(value, list):
        return [_redact_for_log(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_for_log(item) for item in value]
    if isinstance(value, str):
        return _redact_url(value)
    return value


TOOL_LOG_REDACT_KEYS = {
    "busi_param",
    "comment",
    "comments",
    "content",
    "curkey",
    "fid",
    "images",
    "items",
    "media",
    "post",
    "raw",
    "summary",
    "text",
    "unikey",
}
TOOL_LOG_COUNT_KEYS = {"comments", "images", "items", "media"}


def _safe_for_tool_log(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if lowered in TOOL_LOG_COUNT_KEYS and isinstance(value, (list, tuple)):
        return {"count": len(value)}
    if (
        lowered in TOOL_LOG_REDACT_KEYS
        or lowered in SENSITIVE_LOG_KEYS
        or "cookie" in lowered
        or "skey" in lowered
        or "secret" in lowered
        or "token" in lowered
    ):
        if isinstance(value, (dict, list, tuple)):
            try:
                return {"redacted": True, "count": len(value)}
            except Exception:
                return "[redacted]"
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): _safe_for_tool_log(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_safe_for_tool_log(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_for_tool_log(item) for item in value]
    if isinstance(value, str):
        return truncate(_redact_url(value), 180)
    return value


def _safe_for_llm(value: Any) -> Any:
    if isinstance(value, dict):
        visible: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if (
                lowered in LLM_INTERNAL_KEYS
                or "cookie" in lowered
                or "skey" in lowered
                or "secret" in lowered
                or "token" in lowered
            ):
                continue
            visible[key_text] = _safe_for_llm(item)
        return visible
    if isinstance(value, list):
        return [_safe_for_llm(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_for_llm(item) for item in value]
    if isinstance(value, str):
        return truncate(_redact_url(value), 500)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return truncate(str(value), 500)


def _public_error_reason(message: Any) -> str:
    text = str(message or "").strip()
    text = re.sub(r"^\s*(?:Result|结果)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\[[A-Z0-9_:-]+\]\s*", "", text)
    text = re.split(r"(?:\n|【对话要求】|请用|严格禁止|不要提)", text, maxsplit=1)[0].strip()
    text = text.strip(" \t\r\n:：-—")
    if not text:
        return "现在还没办法继续"
    return truncate(text, 80)


def _public_error_detail_parts(detail: Any) -> list[str]:
    if not isinstance(detail, dict):
        return []
    parts: list[str] = []
    status_code = detail.get("status_code")
    if status_code is not None:
        parts.append(f"HTTP {status_code}")
    returncode = detail.get("returncode")
    if returncode is not None:
        parts.append(f"退出码 {returncode}")
    daemon_port = detail.get("daemon_port")
    if daemon_port:
        parts.append(f"daemon 端口 {daemon_port}")
    location = detail.get("location")
    if location:
        parts.append(f"跳转 {_redact_url(str(location))}")
    url = detail.get("url")
    if url:
        parts.append(f"地址 {_redact_url(str(url))}")
    if detail.get("log_path"):
        parts.append("daemon 日志可在插件数据目录查看")
    attempts = detail.get("attempts")
    if isinstance(attempts, list) and attempts:
        parts.append(f"启动尝试 {len(attempts)} 次")
        last_attempt = attempts[-1]
        if isinstance(last_attempt, dict):
            parts.extend(_public_error_detail_parts(last_attempt))
    if detail.get("text") or detail.get("raw") or detail.get("log_tail"):
        parts.append("响应详情已隐藏")
    return parts


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _chmod_private_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _path_contains(parent: Path, child: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except Exception:
        return False


@contextmanager
def _migration_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(data_dir)
    lock_path = data_dir / LEGACY_MIGRATION_LOCK
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0)
        if not lock_file.read(1):
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_plugin_name(plugin_root: Path) -> str:
    metadata = plugin_root / "metadata.yaml"
    try:
        for line in metadata.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("name:"):
                continue
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            if value:
                return value
    except Exception:
        pass
    return PLUGIN_DATA_NAME


def _star_tools_data_dir(plugin_name: str) -> Path | None:
    try:
        from astrbot.api.star import StarTools
    except ImportError as exc:
        logger.warning("qzone StarTools unavailable; using legacy data dir: %s", exc)
        return None
    try:
        return Path(StarTools.get_data_dir(plugin_name))
    except Exception as exc:
        logger.warning("qzone StarTools data dir unavailable; using legacy data dir: %s", exc)
        return None


def _safe_copy_legacy_file(source: Path, target: Path, *, legacy_root: Path, data_dir: Path) -> str:
    if source.is_symlink():
        return "skipped_symlink"
    if not source.is_file():
        return "skipped_not_file"
    if not _path_contains(legacy_root, source):
        return "skipped_source_outside_legacy"
    if not _path_contains(data_dir, target):
        return "skipped_target_outside_data_dir"
    if target.exists():
        return "skipped_target_exists"
    tmp = target.with_name(f"{target.name}.tmp.{int(time.time() * 1000)}.{random.randrange(1000000):06d}")
    try:
        shutil.copyfile(source, tmp)
        _chmod_private(tmp)
        tmp.replace(target)
        _chmod_private(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
    return "copied"


def _write_json_private(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{int(time.time() * 1000)}.{random.randrange(1000000):06d}")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        _chmod_private(tmp)
        tmp.replace(path)
        _chmod_private(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass


def _migrate_legacy_data_dir(legacy_dir: Path, data_dir: Path) -> None:
    try:
        legacy = legacy_dir.resolve()
        target = data_dir.resolve()
    except Exception:
        legacy = legacy_dir
        target = data_dir
    if legacy == target or not legacy_dir.exists() or not legacy_dir.is_dir():
        return
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private_dir(data_dir)
    except Exception as exc:
        logger.warning("qzone standard data dir is not writable: %s", exc)
        return
    try:
        with _migration_lock(data_dir):
            sentinel = data_dir / LEGACY_MIGRATION_SENTINEL
            if sentinel.exists():
                try:
                    marker = json.loads(sentinel.read_text(encoding="utf-8"))
                    if isinstance(marker, dict) and marker.get("complete") is True:
                        return
                except Exception:
                    pass
            results: dict[str, str] = {}
            for name in LEGACY_MIGRATION_FILES:
                source = legacy_dir / name
                if not source.exists():
                    results[name] = "skipped_missing"
                    continue
                try:
                    results[name] = _safe_copy_legacy_file(
                        source,
                        data_dir / name,
                        legacy_root=legacy_dir,
                        data_dir=data_dir,
                    )
                except Exception as exc:
                    results[name] = f"failed_{type(exc).__name__}"
                    logger.warning("qzone legacy data migration skipped %s: %s", name, exc)
            payload = {
                "complete": not any(status.startswith("failed_") for status in results.values()),
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "legacy_dir": str(legacy_dir),
                "data_dir": str(data_dir),
                "files": results,
                "legacy_cleanup_recommended": True,
            }
            _write_json_private(sentinel, payload)
            if any(status == "copied" for status in results.values()):
                logger.warning("qzone legacy data copied to AstrBot data dir; review and remove old data/qzone when safe")
    except Exception as exc:
        logger.warning("qzone legacy data migration failed: %s", exc)


def _standard_data_dir(plugin_root: Path) -> Path:
    plugin_name = _read_plugin_name(plugin_root)
    data_dir = _star_tools_data_dir(plugin_name)
    if data_dir is None:
        return plugin_root / "data" / "qzone"
    _migrate_legacy_data_dir(plugin_root / "data" / "qzone", data_dir)
    return data_dir


def _local_qzone_bridge_root() -> Path:
    package_root = (PLUGIN_ROOT / "qzone_bridge").resolve(strict=False)
    package_init = package_root / "__init__.py"
    if not package_init.is_file():
        raise RuntimeError(f"qzone_bridge package is missing: {package_init}")
    return package_root


def _verify_local_qzone_bridge_module(name: str, package_root: Path) -> None:
    module = sys.modules.get(name)
    if module is None:
        return
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise RuntimeError(f"{name} is already loaded without a file path")
    package_path = Path(module_file).resolve(strict=False)
    if not _path_contains(package_root, package_path):
        raise RuntimeError(f"{name} resolved outside plugin directory: {package_path}")


def _qzone_bridge_contract_is_current(package_root: Path) -> bool:
    package = sys.modules.get("qzone_bridge")
    if package is None:
        return False
    _verify_local_qzone_bridge_module("qzone_bridge", package_root)
    try:
        if int(getattr(package, "BRIDGE_API_VERSION", 0) or 0) < REQUIRED_QZONE_BRIDGE_API_VERSION:
            return False
    except (TypeError, ValueError):
        return False
    return True


def _evict_local_qzone_bridge_modules(package_root: Path) -> None:
    names = [
        name
        for name in sys.modules
        if name == "qzone_bridge" or name.startswith("qzone_bridge.")
    ]
    for name in sorted(names, key=lambda item: item.count("."), reverse=True):
        _verify_local_qzone_bridge_module(name, package_root)
        sys.modules.pop(name, None)


def _load_local_qzone_bridge_package() -> None:
    package_root = _local_qzone_bridge_root()
    for name in tuple(sys.modules):
        if name == "qzone_bridge" or name.startswith("qzone_bridge."):
            _verify_local_qzone_bridge_module(name, package_root)

    if "qzone_bridge" in sys.modules and _qzone_bridge_contract_is_current(package_root):
        return
    if "qzone_bridge" in sys.modules:
        _evict_local_qzone_bridge_modules(package_root)

    package_init = package_root / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "qzone_bridge",
        package_init,
        submodule_search_locations=[str(package_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load qzone_bridge package from {package_init}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["qzone_bridge"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if sys.modules.get("qzone_bridge") is module:
            sys.modules.pop("qzone_bridge", None)
        raise
    _verify_local_qzone_bridge_module("qzone_bridge", package_root)


_load_local_qzone_bridge_package()

from qzone_bridge.controller import QzoneDaemonController
from qzone_bridge.errors import DaemonUnavailableError, QzoneBridgeError, QzoneCookieAcquireError, QzoneNeedsRebind
from qzone_bridge.llm import QzoneLLM
from qzone_bridge.media import PostMedia, PostPayload, collect_post_payload, normalize_media_list, source_name
from qzone_bridge.models import FeedEntry
from qzone_bridge.onebot_cookie import fetch_cookie_text
from qzone_bridge.parser import normalize_uin, parse_cookie_text
from qzone_bridge.post_service import QzonePostService
from qzone_bridge.posts import PostStore
import qzone_bridge.publish_renderer as _publish_renderer
try:
    import qzone_bridge.compat as _bridge_compat
except Exception:
    _bridge_compat = None
from qzone_bridge.render import (
    format_action_result,
    format_feed_detail,
    format_feed_list,
    format_like_result,
    format_llm_feed_list,
    format_status,
)
from qzone_bridge.scheduler import cron_delay_seconds
import qzone_bridge.selection as _selection
from qzone_bridge.settings import PluginSettings
import qzone_bridge.social as _social
from qzone_bridge.utils import truncate

from .core.campus_wall import CampusWall
from .core.config import PluginConfig
from .core.db import PostDB
from .core.model import Post
from .core.sender import Sender
from .core.utils import get_image_urls


class _CompatRenderProfile:
    def __init__(self, nickname: str = "", user_id: str = "", avatar_source: str = "", time_text: str = ""):
        self.nickname = nickname
        self.user_id = user_id
        self.avatar_source = avatar_source
        self.time_text = time_text


def _profile_from_event_fallback(event: Any) -> Any:
    nickname = ""
    for getter_name in ("get_sender_name", "get_sender_nickname"):
        getter = getattr(event, getter_name, None)
        if callable(getter):
            try:
                nickname = str(getter() or "").strip()
            except Exception:
                nickname = ""
            if nickname:
                break
    return RenderProfile(nickname=nickname or "QQ Space", time_text=datetime.now().strftime("%H:%M"))


def _missing_publish_renderer(*args: Any, **kwargs: Any) -> Path:
    raise RuntimeError("qzone publish renderer is unavailable; falling back to text")


RenderProfile = getattr(_publish_renderer, "RenderProfile", _CompatRenderProfile)
cached_avatar_source = getattr(_publish_renderer, "cached_avatar_source", lambda cache_dir, profile: "")
preload_static_render_assets = getattr(_publish_renderer, "preload_static_render_assets", lambda: None)
profile_from_event = getattr(_publish_renderer, "profile_from_event", _profile_from_event_fallback)
render_publish_result_image = getattr(_publish_renderer, "render_publish_result_image", _missing_publish_renderer)
preload_publish_render_assets = getattr(
    _publish_renderer,
    "preload_publish_render_assets",
    lambda profile, cache_dir, **kwargs: profile,
)

PostSelection = _selection.PostSelection
parse_post_selection = _selection.parse_post_selection
selection_from_tool_args = _selection.selection_from_tool_args

QzoneComment = _social.QzoneComment
QzonePost = _social.QzonePost
post_from_entry = _social.post_from_entry


def _extract_nickname_compat(raw: dict[str, Any] | None, *, hostuin: int = 0) -> str:
    helper = getattr(_bridge_compat, "extract_nickname_compat", None)
    if callable(helper):
        return helper(raw, hostuin=hostuin, social_module=_social)
    extractor = getattr(_social, "extract_nickname", None)
    if callable(extractor):
        try:
            nickname = str(extractor(raw, hostuin=hostuin) or "").strip()
        except Exception:
            nickname = ""
        text = re.sub(r"<[^>]+>", "", re.sub(r"\[em\].*?\[/em\]", "", nickname)).strip()
        if text and (not hostuin or text != str(hostuin)) and not re.fullmatch(r"\d{5,}", text):
            return text
    if isinstance(raw, dict):
        stack: list[Any] = [raw]
        for _ in range(48):
            if not stack:
                break
            item = stack.pop(0)
            if isinstance(item, dict):
                owner = int(item.get("uin") or item.get("hostuin") or item.get("user_id") or 0)
                if not hostuin or not owner or owner == hostuin:
                    for key in ("nickname", "nickName", "name", "ownerName"):
                        value = re.sub(r"<[^>]+>", "", re.sub(r"\[em\].*?\[/em\]", "", str(item.get(key) or ""))).strip()
                        if value and (not hostuin or value != str(hostuin)) and not re.fullmatch(r"\d{5,}", value):
                            return value
                stack.extend(value for value in item.values() if isinstance(value, (dict, list)))
            elif isinstance(item, list):
                stack.extend(value for value in item if isinstance(value, (dict, list)))
    return ""


def _selection_has_explicit_input(selection: Any) -> bool:
    helper = getattr(_bridge_compat, "selection_has_explicit_input", None)
    if callable(helper):
        return bool(helper(selection))
    for attribute in (
        "has_explicit_input",
        "explicit_target",
        "explicit_selector",
        "explicit_comment_text",
        "fid",
        "comment_text",
        "target_uin",
    ):
        try:
            if bool(getattr(selection, attribute, False)):
                return True
        except Exception:
            pass
    try:
        selector = str(getattr(selection, "selector", "") or "").strip().lower()
        explicit_range = (
            int(getattr(selection, "start", 1) or 1) != 1
            or int(getattr(selection, "end", 1) or 1) != 1
        )
        return bool(selector and selector != "latest") or explicit_range
    except Exception:
        return False


def _minimal_combine_rendered_post_cards(paths: list[Path], output_dir: Path) -> Path | None:
    if len(paths) <= 1:
        return paths[0] if paths else None
    try:
        import uuid
        from PIL import Image, UnidentifiedImageError
    except Exception:
        return None
    images: list[Any] = []
    try:
        for path in paths:
            try:
                with Image.open(path) as opened:
                    images.append(opened.convert("RGB").copy())
            except (OSError, UnidentifiedImageError):
                return None
        width = max((image.width for image in images), default=0)
        if not width:
            return None
        gap = max(12, min(32, width // 40))
        height = sum(image.height for image in images) + gap * (len(images) - 1)
        canvas = Image.new("RGB", (width, height), (255, 255, 255))
        y = 0
        for image in images:
            canvas.paste(image, (0, y))
            y += image.height + gap
        output_dir.mkdir(parents=True, exist_ok=True)
        prune = getattr(_publish_renderer, "_prune_output_dir", None)
        if callable(prune):
            prune(output_dir)
        output_path = output_dir / f"publish_result_{int(time.time())}_{uuid.uuid4().hex[:10]}_cards.png"
        canvas.save(output_path, "PNG", optimize=False, compress_level=1)
        canvas.close()
        return output_path
    finally:
        for image in images:
            try:
                image.close()
            except Exception:
                pass


def _combine_rendered_post_cards(paths: list[Path], output_dir: Path) -> Path | None:
    helper = getattr(_bridge_compat, "combine_rendered_post_cards_compat", None)
    if callable(helper):
        return helper(paths, output_dir, renderer_module=_publish_renderer)
    combiner = getattr(_publish_renderer, "combine_rendered_post_cards", None)
    if callable(combiner):
        return combiner(paths, output_dir)
    return _minimal_combine_rendered_post_cards(paths, output_dir)


def _render_publish_result_image(*args: Any, fixed_width: bool = False, **kwargs: Any) -> Path:
    if not fixed_width:
        return render_publish_result_image(*args, **kwargs)
    try:
        return render_publish_result_image(*args, fixed_width=fixed_width, **kwargs)
    except TypeError as exc:
        if "fixed_width" not in str(exc):
            raise
        return render_publish_result_image(*args, **kwargs)


def _identity_filter_decorator(*args: Any, **kwargs: Any):
    def decorator(func):
        return func

    return decorator


if not hasattr(filter, "command"):
    setattr(filter, "command", _identity_filter_decorator)
if not hasattr(filter, "permission_type"):
    setattr(filter, "permission_type", _identity_filter_decorator)
if not hasattr(filter, "PermissionType"):
    setattr(filter, "PermissionType", type("PermissionType", (), {"ADMIN": "admin"}))


class QzonePlugin(Star):
    def __init__(self, context: Context, config: Any | None = None):
        super().__init__(context)
        self._context = context
        raw_config = config if config is not None else getattr(context, "get_config", lambda: {})()
        self.settings = PluginSettings.from_mapping(raw_config)
        self.root = Path(__file__).resolve().parent
        self.data_dir = _standard_data_dir(self.root)
        self._onebot_client: Any | None = None
        self._cookie_lock: asyncio.Lock | None = None
        self.controller = QzoneDaemonController(
            plugin_root=self.root,
            data_dir=self.data_dir,
            default_port=self.settings.daemon_port,
            request_timeout=self.settings.request_timeout,
            start_timeout=self.settings.start_timeout,
            keepalive_interval=self.settings.keepalive_interval,
            user_agent=self.settings.user_agent,
            auto_start_daemon=self.settings.auto_start_daemon,
        )
        self._capture_onebot_client_from_context()
        self._daemon_warmup_task: asyncio.Task | None = None
        self._auto_bind_bootstrap_task: asyncio.Task | None = None
        self._auto_bind_bootstrap_succeeded = False
        self._scheduled_tasks: list[asyncio.Task] = []
        self._publisher_profile_cache: tuple[int, RenderProfile] | None = None
        self._publisher_profile_preload_task: asyncio.Task | None = None
        self.posts = PostStore(self.data_dir / "posts.json")
        self.llm = QzoneLLM(self._context, self.settings)
        self._pillowmd_style: Any | None = None
        self._pillowmd_style_dir = ""
        preload_static_render_assets()

        self._legacy_cfg = PluginConfig(raw_config, context)
        self._legacy_cfg.client = self._onebot_client
        self.db = PostDB(self._legacy_cfg)
        self.sender = Sender(self._legacy_cfg)
        self.campus_wall = CampusWall(self._legacy_cfg, self.controller, self.db, self.sender)

    def _sender_id(self, event: AstrMessageEvent) -> int:
        try:
            if hasattr(event, "get_sender_id"):
                value = event.get_sender_id()
                if value is not None:
                    return int(value)
        except Exception:
            pass
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        return int(getattr(sender, "user_id", 0) or 0)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if hasattr(event, "is_admin") and event.is_admin():
                return True
        except Exception:
            pass
        return self._sender_id(event) in set(self.settings.admin_uins)

    def _command_result(self, event: AstrMessageEvent, text: str):
        self._stop_event(event)
        return event.plain_result(text)

    def _post_store(self) -> PostStore:
        expected = self.data_dir / "posts.json"
        if getattr(self.posts, "path", None) != expected:
            self.posts = PostStore(expected)
        return self.posts

    def _post_service(self) -> QzonePostService:
        return QzonePostService(
            self.controller,
            self._post_store(),
            max_feed_limit=self.settings.max_feed_limit,
        )

    def _llm_adapter(self) -> QzoneLLM:
        self.llm.context = getattr(self, "_context", None) or getattr(self, "context", None)
        self.llm.settings = self.settings
        return self.llm

    @staticmethod
    def _onebot_file_uri(path: Path) -> str:
        try:
            return path.resolve().as_uri()
        except ValueError:
            return "file:///" + path.resolve().as_posix().lstrip("/")

    async def _render_markdown_image(self, text: str, subdir: str = "markdown") -> Path | None:
        style_dir = str(self.settings.pillowmd_style_dir or "").strip()
        if not style_dir:
            return None
        try:
            import pillowmd

            if self._pillowmd_style is None or self._pillowmd_style_dir != style_dir:
                self._pillowmd_style = pillowmd.LoadMarkdownStyles(style_dir)
                self._pillowmd_style_dir = style_dir
            output_dir = self.data_dir / "pillowmd" / subdir
            output_dir.mkdir(parents=True, exist_ok=True)
            rendered = await self._pillowmd_style.AioRender(text=text, useImageUrl=True)
            return Path(rendered.Save(output_dir))
        except Exception as exc:
            logger.debug("qzone pillowmd render failed: %s", exc)
            return None

    async def _markdown_result(self, event: AstrMessageEvent, text: str, subdir: str = "markdown"):
        image_path = await self._render_markdown_image(text, subdir=subdir)
        image_result = getattr(event, "image_result", None)
        if image_path is not None and callable(image_result):
            self._stop_event(event)
            return image_result(str(image_path))
        return self._command_result(event, text)

    def _render_asset_dir(self) -> Path:
        return self.data_dir / "render_assets"

    @staticmethod
    def _clone_render_profile(profile: RenderProfile, *, time_text: str = "") -> RenderProfile:
        return RenderProfile(
            nickname=profile.nickname,
            user_id=profile.user_id,
            avatar_source=profile.avatar_source,
            time_text=time_text or profile.time_text,
        )

    @staticmethod
    def _qlogo_url(uin: int, size: int) -> str:
        return f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s={size}"

    def _publisher_avatar_sources(
        self,
        login_uin: int,
        *,
        primary: str = "",
        onebot_avatar: str = "",
    ) -> tuple[str, ...]:
        candidates = [
            primary,
            onebot_avatar,
            self._qlogo_url(login_uin, 640),
            self._qlogo_url(login_uin, 140),
            self._qlogo_url(login_uin, 100),
        ]
        result: list[str] = []
        for source in candidates:
            source = str(source or "").strip()
            if source and source not in result:
                result.append(source)
        return tuple(result)

    def _cached_publisher_profile(self, login_uin: int, *, time_text: str) -> RenderProfile | None:
        cached = self._publisher_profile_cache
        if cached is None:
            return None
        cached_uin, cached_profile = cached
        if cached_uin != login_uin:
            return None
        return self._clone_render_profile(cached_profile, time_text=time_text)

    async def _publisher_render_profile(
        self,
        event: AstrMessageEvent | None = None,
        *,
        status: dict[str, Any] | None = None,
        allow_network: bool = False,
    ) -> RenderProfile:
        profile = profile_from_event(event) if event is not None else RenderProfile(time_text=time.strftime("%H:%M"))
        if status is None:
            try:
                status = await self.controller.get_status(probe_daemon=False)
            except QzoneBridgeError:
                status = {}

        login_uin = int((status or {}).get("login_uin") or 0)
        if not login_uin:
            return profile

        cached = self._cached_publisher_profile(login_uin, time_text=profile.time_text)
        if cached is not None:
            return cached

        nickname = str(
            (status or {}).get("login_nickname")
            or (status or {}).get("nickname")
            or (status or {}).get("publisher_nickname")
            or ""
        ).strip()
        avatar_source = str((status or {}).get("login_avatar") or (status or {}).get("avatar") or "").strip()
        onebot_avatar = ""
        if allow_network:
            bot = self._capture_onebot_client(event)
            if bot is not None:
                try:
                    fetched = await asyncio.wait_for(self._fetch_onebot_user_info(bot, login_uin), timeout=1.2)
                except Exception:
                    fetched = {}
                if fetched:
                    nickname = nickname or str(fetched.get("nickname") or fetched.get("name") or "").strip()
                    onebot_avatar = str(fetched.get("avatar") or fetched.get("avatar_url") or "").strip()
                    avatar_source = onebot_avatar or avatar_source

        fallback_name = "" if profile.nickname == "QQ Space" else profile.nickname
        base_profile = RenderProfile(
            nickname=nickname or fallback_name or str(login_uin),
            user_id=str(login_uin),
            avatar_source=avatar_source or self._qlogo_url(login_uin, 640),
            time_text=profile.time_text,
        )
        cached_avatar = cached_avatar_source(self._render_asset_dir(), base_profile)
        if cached_avatar:
            cached_profile = RenderProfile(
                nickname=base_profile.nickname,
                user_id=base_profile.user_id,
                avatar_source=cached_avatar,
                time_text="",
            )
            self._publisher_profile_cache = (login_uin, cached_profile)
            return self._clone_render_profile(cached_profile, time_text=profile.time_text)

        if not allow_network:
            base_profile.avatar_source = ""
            return base_profile

        sources = self._publisher_avatar_sources(login_uin, primary=base_profile.avatar_source, onebot_avatar=onebot_avatar)
        preloaded = await asyncio.to_thread(
            preload_publish_render_assets,
            base_profile,
            self._render_asset_dir(),
            avatar_sources=sources,
            remote_timeout=max(float(self.settings.render_remote_timeout or 0), 2.5),
        )
        cached_profile = RenderProfile(
            nickname=preloaded.nickname or str(login_uin),
            user_id=preloaded.user_id or str(login_uin),
            avatar_source=preloaded.avatar_source,
            time_text="",
        )
        self._publisher_profile_cache = (login_uin, cached_profile)
        return self._clone_render_profile(cached_profile, time_text=profile.time_text)

    async def _fetch_onebot_user_info(self, bot: Any, uin: int) -> dict[str, Any]:
        for method_name, kwargs in (
            ("get_stranger_info", {"user_id": uin, "no_cache": False}),
            ("get_friend_info", {"user_id": uin}),
            ("get_user_info", {"user_id": uin}),
        ):
            method = getattr(bot, method_name, None)
            if not callable(method):
                continue
            try:
                result = method(**kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
            except TypeError:
                try:
                    result = method(uin)
                    if asyncio.iscoroutine(result):
                        result = await result
                except Exception:
                    continue
            except Exception:
                continue
            if isinstance(result, dict):
                return result
        return {}

    def _schedule_publish_render_asset_preload(
        self,
        trigger: str,
        *,
        event: AstrMessageEvent | None = None,
        status: dict[str, Any] | None = None,
    ) -> None:
        if not self.settings.render_publish_result:
            return
        login_uin = int((status or {}).get("login_uin") or 0)
        if login_uin and self._cached_publisher_profile(login_uin, time_text="") is not None:
            return
        task = self._publisher_profile_preload_task
        if task is not None and not task.done():
            return

        async def runner() -> None:
            try:
                await self._publisher_render_profile(event, status=status, allow_network=True)
            except Exception:
                logger.debug("qzone publish render asset preload on %s failed", trigger, exc_info=True)

        self._publisher_profile_preload_task = asyncio.create_task(runner())

    def _schedule_publisher_profile(self, event: AstrMessageEvent) -> asyncio.Task | None:
        if not self.settings.render_publish_result:
            return None
        return asyncio.create_task(self._publisher_render_profile(event, allow_network=False))

    async def _publish_result(
        self,
        event: AstrMessageEvent,
        post: PostPayload,
        payload: dict[str, Any],
        *,
        profile_task: asyncio.Task | None = None,
    ):
        text = format_action_result("发布结果", payload)
        if not self.settings.render_publish_result:
            self._stop_event(event)
            return event.plain_result(text)
        try:
            profile = await profile_task if profile_task is not None else await self._publisher_render_profile(event)
        except Exception:
            profile = profile_from_event(event)
        try:
            image_path = await asyncio.to_thread(
                _render_publish_result_image,
                post,
                self.data_dir / "rendered_posts",
                profile=profile,
                result=payload,
                width=self.settings.render_result_width,
                remote_timeout=self.settings.render_remote_timeout,
            )
        except Exception as exc:
            logger.exception("qzone publish result render failed: %s", exc)
            self._stop_event(event)
            return event.plain_result(text)

        image_result = getattr(event, "image_result", None)
        if callable(image_result):
            self._stop_event(event)
            return image_result(str(image_path))
        self._stop_event(event)
        return event.plain_result(f"{text}\n图片路径: {image_path}")

    def _post_render_limit(self) -> int:
        limit = int(getattr(self.settings, "render_feed_card_limit", 5) or 5)
        return max(1, min(limit, self.settings.max_feed_limit))

    @staticmethod
    def _post_display_nickname(post: QzonePost) -> str:
        hostuin = int(post.hostuin or 0)
        nickname = _extract_nickname_compat({"nickname": post.nickname}, hostuin=hostuin)
        if not nickname:
            nickname = _extract_nickname_compat(post.raw, hostuin=hostuin)
        return nickname or "QQ 空间用户"

    @staticmethod
    def _post_time_text(post: QzonePost) -> str:
        created_at = int(post.created_at or 0)
        if created_at <= 0:
            return UNKNOWN_POST_TIME_TEXT
        try:
            created = datetime.fromtimestamp(created_at)
        except (OSError, OverflowError, ValueError):
            return UNKNOWN_POST_TIME_TEXT
        if created.date() == datetime.now().date():
            return created.strftime("%H:%M")
        return created.strftime("%m-%d %H:%M")

    def _post_render_profile(self, post: QzonePost) -> RenderProfile:
        user_id = str(post.hostuin or "")
        return RenderProfile(
            nickname=self._post_display_nickname(post),
            user_id=user_id,
            avatar_source=self._qlogo_url(post.hostuin, 640) if post.hostuin else "",
            time_text=self._post_time_text(post),
        )

    @staticmethod
    def _post_render_payload(post: QzonePost) -> PostPayload:
        media = [
            PostMedia(kind="image", source=str(source), name=source_name(str(source)))
            for source in post.images[:9]
            if str(source or "").strip()
        ]
        return PostPayload(content=(post.summary or "(空)").strip(), media=media)

    async def _render_qzone_post_card(
        self,
        post: QzonePost,
        *,
        fixed_width: bool = False,
        comment_text: str = "",
    ) -> Path | None:
        if not self.settings.render_publish_result:
            return None
        result: dict[str, Any] = {"ok": True, "tool": "qzone_post_card", "fid": post.fid}
        if str(comment_text or "").strip():
            result["comment"] = truncate(str(comment_text).strip(), 220)
        try:
            return await asyncio.to_thread(
                _render_publish_result_image,
                self._post_render_payload(post),
                self.data_dir / "rendered_posts",
                profile=self._post_render_profile(post),
                result=result,
                width=self.settings.render_result_width,
                remote_timeout=self.settings.render_remote_timeout,
                fixed_width=fixed_width,
            )
        except Exception as exc:
            logger.exception("qzone post card render failed: %s", exc)
            return None

    async def _render_qzone_post_cards(
        self,
        posts: list[QzonePost],
        *,
        comment_texts: dict[int, str] | None = None,
    ) -> list[Path]:
        if not posts:
            return []
        if len(posts) == 1:
            path = await self._render_qzone_post_card(posts[0], comment_text=(comment_texts or {}).get(id(posts[0]), ""))
            return [path] if path is not None else []

        semaphore = asyncio.Semaphore(min(3, len(posts)))

        async def render_one(post: QzonePost) -> Path | None:
            async with semaphore:
                return await self._render_qzone_post_card(
                    post,
                    fixed_width=True,
                    comment_text=(comment_texts or {}).get(id(post), ""),
                )

        rendered = await asyncio.gather(*(render_one(post) for post in posts))
        return [path for path in rendered if path is not None]

    async def _post_card_results(
        self,
        event: AstrMessageEvent,
        posts: list[QzonePost],
        fallback_text: str,
        *,
        subdir: str = "posts",
        fallback_when_unrendered: bool = True,
        comment_texts: dict[int, str] | None = None,
    ) -> list[Any]:
        if not posts:
            return [self._command_result(event, fallback_text)] if fallback_when_unrendered else []
        image_result = getattr(event, "image_result", None)
        if not self.settings.render_publish_result or not callable(image_result):
            if fallback_when_unrendered:
                return [await self._markdown_result(event, fallback_text, subdir=subdir)]
            return []

        results: list[Any] = []
        limit = self._post_render_limit()
        image_paths = await self._render_qzone_post_cards(posts[:limit], comment_texts=comment_texts)
        if len(image_paths) > 1:
            try:
                combined_path = await asyncio.to_thread(
                    _combine_rendered_post_cards,
                    image_paths,
                    self.data_dir / "rendered_posts",
                )
            except Exception as exc:
                logger.exception("qzone post card merge failed: %s", exc)
                combined_path = None
            if combined_path is not None:
                image_paths = [combined_path]
            elif fallback_when_unrendered:
                return [await self._markdown_result(event, fallback_text, subdir=subdir)]
            else:
                return [self._command_result(event, "说说卡片图片合成失败，请缩小范围后重试。")]
        for image_path in image_paths:
            self._stop_event(event)
            results.append(image_result(str(image_path)))

        if not results and fallback_when_unrendered:
            return [await self._markdown_result(event, fallback_text, subdir=subdir)]
        if len(posts) > limit:
            results.append(self._command_result(event, f"已渲染前 {limit} 条说说，其余内容请缩小范围后查看。"))
        return results

    async def _yield_post_card_results(
        self,
        event: AstrMessageEvent,
        posts: list[QzonePost],
        fallback_text: str,
        *,
        subdir: str = "posts",
        fallback_when_unrendered: bool = True,
        comment_texts: dict[int, str] | None = None,
    ):
        for result in await self._post_card_results(
            event,
            posts,
            fallback_text,
            subdir=subdir,
            fallback_when_unrendered=fallback_when_unrendered,
            comment_texts=comment_texts,
        ):
            yield result

    async def _notify_admin_post_card(
        self,
        event: AstrMessageEvent | None,
        post: QzonePost,
        message: str,
        *,
        comment_text: str = "",
    ) -> None:
        if not self.settings.send_admin:
            return
        bot = self._capture_onebot_client(event)
        if bot is None:
            return
        try:
            image_path = await self._render_qzone_post_card(post, comment_text=comment_text)
        except TypeError as exc:
            if "comment_text" not in str(exc):
                raise
            image_path = await self._render_qzone_post_card(post)
        outgoing: Any = message
        if image_path is not None:
            outgoing = [
                {"type": "text", "data": {"text": f"{message}\n"}},
                {"type": "image", "data": {"file": self._onebot_file_uri(image_path)}},
            ]
        await self._send_admin_outgoing(bot, outgoing)

    async def _notify_admin_publish_result(
        self,
        post: PostPayload,
        payload: dict[str, Any],
        message: str,
    ) -> None:
        if not getattr(self.settings, "send_admin", False):
            return
        bot = self._capture_onebot_client(None)
        if bot is None:
            return
        result_text = format_action_result("发布结果", payload)
        outgoing: Any = f"{message}\n{result_text}"
        if getattr(self.settings, "render_publish_result", True):
            try:
                profile = await self._publisher_render_profile(None, allow_network=False)
            except Exception:
                profile = RenderProfile(time_text=time.strftime("%H:%M"))
            try:
                image_path = await asyncio.to_thread(
                    _render_publish_result_image,
                    post,
                    self.data_dir / "rendered_posts",
                    profile=profile,
                    result=payload,
                    width=int(getattr(self.settings, "render_result_width", 900) or 900),
                    remote_timeout=float(getattr(self.settings, "render_remote_timeout", 0.35) or 0.35),
                )
            except Exception as exc:
                logger.exception("qzone scheduled publish result render failed: %s", exc)
            else:
                outgoing = [
                    {"type": "text", "data": {"text": f"{message}\n"}},
                    {"type": "image", "data": {"file": self._onebot_file_uri(image_path)}},
                ]
        await self._send_admin_outgoing(bot, outgoing)

    @staticmethod
    def _coerce_uin_targets(values: Any) -> list[int]:
        if values is None:
            return []
        if isinstance(values, str):
            items: Any = values.split(",")
        elif isinstance(values, (list, tuple, set)):
            items = values
        else:
            items = [values]
        targets: list[int] = []
        seen: set[int] = set()
        for item in items:
            text = str(item or "").strip()
            if not text.isdigit():
                continue
            target = int(text)
            if target > 0 and target not in seen:
                targets.append(target)
                seen.add(target)
        return targets

    def _global_admin_targets(self) -> list[int]:
        context = getattr(self, "_context", None) or getattr(self, "context", None)
        if context is None:
            return []
        try:
            config = context.get_config()
        except Exception:
            return []
        getter = getattr(config, "get", None)
        if callable(getter):
            return self._coerce_uin_targets(getter("admins_id", []))
        return self._coerce_uin_targets(getattr(config, "admins_id", []))

    def _admin_private_targets(self) -> tuple[list[int], str]:
        configured = self._coerce_uin_targets(getattr(self.settings, "admin_uins", []))
        if configured:
            return configured, "admin_uins"
        global_admins = self._global_admin_targets()
        if global_admins:
            return global_admins, "admins_id"
        return [], ""

    async def _call_onebot_action(self, bot: Any, action: str, **kwargs: Any) -> None:
        method = getattr(bot, action, None)
        if callable(method):
            await self._maybe_await(method(**kwargs))
            return
        call_action = getattr(bot, "call_action", None)
        if callable(call_action):
            await self._maybe_await(call_action(action, **kwargs))
            return
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        if callable(call_action):
            await self._maybe_await(call_action(action, **kwargs))
            return
        raise AttributeError(f"OneBot client does not support {action}")

    async def _send_admin_outgoing(self, bot: Any, outgoing: Any) -> int:
        sent = 0
        manage_group = int(getattr(self.settings, "manage_group", 0) or 0)
        admin_targets, admin_source = self._admin_private_targets()
        if manage_group:
            try:
                await self._call_onebot_action(bot, "send_group_msg", group_id=manage_group, message=outgoing)
            except Exception as exc:
                logger.warning("qzone admin notification group send failed group_id=%s: %s", manage_group, exc)
            else:
                return 1

        for admin in admin_targets:
            try:
                await self._call_onebot_action(bot, "send_private_msg", user_id=admin, message=outgoing)
            except Exception as exc:
                logger.warning("qzone admin notification private send failed user_id=%s: %s", admin, exc)
                continue
            sent += 1

        return sent

    def _stop_event(self, event: AstrMessageEvent) -> None:
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            try:
                stopper()
            except Exception:
                pass

    def _error_text(self, exc: QzoneBridgeError) -> str:
        if not exc.detail:
            return exc.message
        parts = _public_error_detail_parts(exc.detail)
        if parts:
            return f"{exc.message}（{', '.join(dict.fromkeys(parts))}）"
        return exc.message

    @staticmethod
    def _format_posts(posts: list[QzonePost], *, detail: bool = False) -> str:
        if not posts:
            return "没有找到可见说说。"
        if detail:
            return "\n\n".join(post.detail_text(post.local_id) for post in posts)
        return "\n\n".join(post.brief(post.local_id) for post in posts)

    @staticmethod
    def _format_visitors(payload: dict[str, Any]) -> str:
        items = payload.get("items") or []
        if not items:
            return "暂时没有访客记录。"
        lines = ["最近访客"]
        for index, item in enumerate(items[:20], 1):
            if not isinstance(item, dict):
                continue
            name = item.get("nickname") or item.get("uin") or "-"
            uin = item.get("uin") or ""
            lines.append(f"{index}. {name} {uin}".strip())
        return "\n".join(lines)

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _event_text(event: AstrMessageEvent) -> str:
        value = getattr(event, "message_str", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        message_obj = getattr(event, "message_obj", None)
        parts: list[str] = []
        for item in getattr(message_obj, "message", []) or []:
            if isinstance(item, str):
                parts.append(item)
                continue
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
                continue
            data = getattr(item, "data", None)
            if isinstance(data, dict) and isinstance(data.get("text"), str):
                parts.append(data["text"])
        return "".join(parts).strip()

    @staticmethod
    def _message_after_command(text: str, names: tuple[str, ...]) -> str:
        text = str(text or "").strip()
        text = re.sub(r"^(?:[!/／]\s*)", "", text).strip()
        for name in sorted(names, key=len, reverse=True):
            if text == name:
                return ""
            if text.startswith(name):
                rest = text[len(name):]
                if not rest or rest[0].isspace() or rest[0] in {":", "："}:
                    return rest.lstrip(" \t:：")
        return text

    def _sender_name(self, event: AstrMessageEvent) -> str:
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
        return str(self._sender_id(event) or "")

    def _group_id(self, event: AstrMessageEvent) -> int:
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                value = getter()
                if value:
                    return int(value)
            except Exception:
                pass
        message_obj = getattr(event, "message_obj", None)
        try:
            return int(getattr(message_obj, "group_id", 0) or 0)
        except Exception:
            return 0

    def _self_id(self, event: AstrMessageEvent) -> int:
        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            try:
                value = getter()
                if value:
                    return int(value)
            except Exception:
                pass
        try:
            status = getattr(self.controller, "store", None).read()
            return int(status.session.uin or 0)
        except Exception:
            return 0

    @staticmethod
    def _at_uins(event: AstrMessageEvent, text: str = "") -> list[int]:
        uins: list[int] = []
        message_obj = getattr(event, "message_obj", None)
        for item in getattr(message_obj, "message", []) or []:
            data = getattr(item, "data", None)
            if isinstance(data, dict):
                value = data.get("qq") or data.get("uin") or data.get("user_id")
                if value and str(value).isdigit():
                    uins.append(int(value))
            for attr in ("qq", "uin", "user_id"):
                value = getattr(item, attr, None)
                if value and str(value).isdigit():
                    uins.append(int(value))
        for match in re.finditer(r"\[CQ:at,qq=(\d+)[^\]]*\]|@(\d{5,})", text):
            value = match.group(1) or match.group(2)
            if value:
                uins.append(int(value))
        deduped: list[int] = []
        for uin in uins:
            if uin not in deduped:
                deduped.append(uin)
        return deduped

    def _selection_for_event(self, event: AstrMessageEvent, names: tuple[str, ...]) -> PostSelection:
        text = self._event_text(event)
        selection = parse_post_selection(text, names)
        if not selection.target_uin:
            at_uins = self._at_uins(event, text)
            if at_uins:
                selection.target_uin = at_uins[0]
        return selection

    async def _posts_for_selection(
        self,
        selection: PostSelection,
        *,
        target_id: int | None = None,
        with_detail: bool = False,
        no_commented: bool = False,
        no_self: bool = False,
        login_uin: int | None = None,
    ) -> list[QzonePost]:
        if target_id is not None:
            selection.target_uin = int(target_id)
        return await self._post_service().resolve_posts(
            selection,
            with_detail=with_detail,
            no_commented=no_commented,
            no_self=no_self,
            login_uin=int(login_uin or 0),
        )

    async def _posts_for_event(
        self,
        event: AstrMessageEvent,
        names: tuple[str, ...],
        *,
        target_id: int | None = None,
        with_detail: bool = False,
        no_commented: bool = False,
        no_self: bool = False,
    ) -> list[QzonePost]:
        return await self._posts_for_selection(
            self._selection_for_event(event, names),
            target_id=target_id,
            with_detail=with_detail,
            no_commented=no_commented,
            no_self=no_self,
            login_uin=self._self_id(event),
        )

    async def _ensure_daemon(self, *, allow_needs_rebind: bool = False) -> None:
        status = await self.controller.get_status()
        if status.get("needs_rebind") and not allow_needs_rebind:
            raise QzoneNeedsRebind("QQ 空间登录态已失效，需要重新绑定 Cookie")
        if allow_needs_rebind:
            if status.get("daemon_state") != "ready":
                await self.controller.ensure_running()
            return
        if self.settings.auto_start_daemon:
            if status.get("daemon_state") != "ready":
                await self.controller.ensure_running()
        elif status.get("daemon_state") != "ready":
            raise DaemonUnavailableError("daemon 未运行")

    def _limit(self, limit: int | None) -> int:
        if not limit or limit <= 0:
            return self.settings.public_feed_limit
        return min(limit, self.settings.max_feed_limit)

    def _to_feed_entries(self, payload: dict[str, Any]) -> list[FeedEntry]:
        items = payload.get("items") or []
        entries: list[FeedEntry] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entries.append(FeedEntry(**item))
        return entries

    async def _generate_post_text(self, event: AstrMessageEvent, topic: str = "") -> str:
        return await self._llm_adapter().generate_post_text(event, topic)

    async def _generate_comment_text(self, event: AstrMessageEvent, post: QzonePost) -> str:
        return await self._llm_adapter().generate_comment_text(event, post)

    async def _generate_reply_text(self, event: AstrMessageEvent, post: QzonePost, comment: QzoneComment) -> str:
        return await self._llm_adapter().generate_reply_text(event, post, comment)

    @staticmethod
    def _cron_delay_seconds(cron: str, offset_seconds: int) -> float:
        return cron_delay_seconds(cron, offset_seconds, now=datetime.now(), randint=random.randint)

    def _start_scheduled_tasks(self) -> None:
        if self._scheduled_tasks:
            return
        if self.settings.publish_cron:
            self._scheduled_tasks.append(
                asyncio.create_task(
                    self._scheduled_loop("publish", self.settings.publish_cron, self.settings.publish_offset, self._auto_publish_once)
                )
            )
        if self.settings.comment_cron:
            self._scheduled_tasks.append(
                asyncio.create_task(
                    self._scheduled_loop("comment", self.settings.comment_cron, self.settings.comment_offset, self._auto_comment_once)
                )
            )
        if self.settings.reply_cron:
            self._scheduled_tasks.append(
                asyncio.create_task(
                    self._scheduled_loop("reply", self.settings.reply_cron, self.settings.reply_offset, self._auto_reply_once)
                )
            )

    async def _scheduled_loop(self, name: str, cron: str, offset: int, action: Any) -> None:
        while True:
            delay = self._cron_delay_seconds(cron, offset)
            if delay <= 0:
                logger.info("qzone scheduled %s disabled: invalid cron=%s", name, cron)
                return
            logger.info("qzone scheduled %s next run in %.1fs cron=%s offset=%s", name, delay, cron, offset)
            await asyncio.sleep(delay)
            try:
                logger.info("qzone scheduled %s run started", name)
                await action()
                logger.info("qzone scheduled %s run finished", name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("qzone scheduled %s failed: %s", name, exc)

    async def _auto_publish_once(self) -> None:
        logger.info("qzone scheduled publish started")
        fake_event = None
        text = await self._generate_post_text(fake_event, "")
        if not text.strip():
            logger.info("qzone scheduled publish skipped: generated content is empty")
            return
        post = PostPayload(content=text.strip(), media=[])
        await self._ensure_cookie_ready()
        await self._ensure_daemon()
        payload = await self.controller.publish_post(content=post.content, content_sanitized=True)
        logger.info(
            "qzone scheduled publish succeeded fid=%s text_length=%s",
            payload.get("fid") or "",
            len(post.content),
        )
        await self._notify_admin_publish_result(post, payload, "定时自动发布完成")

    def _scheduled_comment_target_count(self) -> int:
        configured = int(getattr(self.settings, "comment_latest_count", 1) or 1)
        max_limit = int(getattr(self.settings, "max_feed_limit", 20) or 20)
        return max(1, min(configured, max(1, max_limit)))

    def _auto_comment_state_path(self) -> Path:
        return self.data_dir / "auto_comment_state.json"

    def _load_auto_comment_keys(self) -> set[str]:
        path = self._auto_comment_state_path()
        if not path.exists():
            return set()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        items = payload.get("commented") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return set()
        return {str(item) for item in items if item}

    def _save_auto_comment_keys(self, keys: set[str]) -> None:
        path = self._auto_comment_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"commented": sorted(keys)[-500:]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _auto_comment_key(post: QzonePost | FeedEntry) -> str:
        return f"{int(getattr(post, 'hostuin', 0) or 0)}:{getattr(post, 'fid', '')}"

    async def _auto_comment_once(self) -> None:
        target_count = self._scheduled_comment_target_count()
        max_limit = max(1, int(getattr(self.settings, "max_feed_limit", 20) or 20))
        fetch_limit = min(max_limit, max(5, target_count * 3))
        logger.info("qzone scheduled comment started target_count=%s fetch_limit=%s", target_count, fetch_limit)
        await self._ensure_cookie_ready()
        await self._ensure_daemon()
        payload = await self.controller.list_feeds(hostuin=0, limit=fetch_limit, scope="active")
        entries = self._to_feed_entries(payload)
        commented_keys = self._load_auto_comment_keys()
        login_uin = 0
        try:
            status = await self.controller.get_status(probe_daemon=False)
            login_uin = int(status.get("login_uin") or 0)
        except Exception:
            pass
        commented = 0
        for entry in entries:
            if commented >= target_count:
                break
            if not entry.fid or not entry.hostuin:
                continue
            if login_uin and entry.hostuin == login_uin:
                continue
            key = self._auto_comment_key(entry)
            if key in commented_keys:
                continue
            try:
                detail_payload = await self.controller.detail_feed(hostuin=entry.hostuin, fid=entry.fid, appid=entry.appid)
                entry_data = detail_payload.get("entry")
                if isinstance(entry_data, dict):
                    detail_entry = FeedEntry(**entry_data)
                    if detail_entry.fid == entry.fid and detail_entry.hostuin == entry.hostuin:
                        entry = detail_entry
            except Exception:
                continue
            post = post_from_entry(entry, detail=(detail_payload or {}).get("raw"), local_id=0)
            if detail_payload and detail_payload.get("comments"):
                post.comments = [
                    QzoneComment(
                        commentid=str(item.get("commentid") or ""),
                        uin=int(item.get("uin") or 0),
                        nickname=str(item.get("nickname") or ""),
                        content=str(item.get("content") or ""),
                    )
                    for item in detail_payload.get("comments") or []
                    if isinstance(item, dict)
                ]
            if login_uin and any(comment.uin == login_uin for comment in post.comments):
                commented_keys.add(key)
                self._save_auto_comment_keys(commented_keys)
                continue
            await self._post_store().upsert_async(post)
            text = await self._generate_comment_text(None, post)
            if not text.strip():
                continue
            comment_text = text.strip()
            await self._post_service().comment_post(post, comment_text)
            commented_keys.add(key)
            self._save_auto_comment_keys(commented_keys)
            commented += 1
            if getattr(self.settings, "like_when_comment", False):
                try:
                    await self._post_service().like_post(post)
                except Exception:
                    pass
            try:
                await self._notify_admin_post_card(
                    None,
                    post,
                    f"定时自动评论了 {self._post_display_nickname(post)} 的说说：{truncate(comment_text, 60)}",
                    comment_text=comment_text,
                )
            except Exception:
                pass
        if commented:
            logger.info("qzone scheduled comment succeeded commented=%s", commented)

    def _auto_reply_state_path(self) -> Path:
        return self.data_dir / "auto_reply_state.json"

    def _load_auto_reply_state(self) -> dict[str, Any]:
        path = self._auto_reply_state_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_auto_reply_state(self, state: dict[str, Any]) -> None:
        path = self._auto_reply_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        recent = dict(sorted(state.items(), key=lambda item: item[1].get("last_reply_at", 0), reverse=True)[:500])
        path.write_text(
            json.dumps(recent, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _reply_depth_for_comment(self, state: dict[str, Any], comment_key: str) -> int:
        entry = state.get(comment_key)
        if not isinstance(entry, dict):
            return 0
        return int(entry.get("depth", 0) or 0)

    def _record_reply(self, state: dict[str, Any], comment_key: str, *, depth: int) -> None:
        state[comment_key] = {
            "depth": depth,
            "last_reply_at": int(time.time()),
        }

    async def _auto_reply_once(self) -> None:
        logger.info("qzone scheduled reply started")
        await self._ensure_cookie_ready()
        await self._ensure_daemon()
        login_uin = 0
        try:
            status = await self.controller.get_status(probe_daemon=False)
            login_uin = int(status.get("login_uin") or 0)
        except Exception:
            pass
        if not login_uin:
            logger.info("qzone scheduled reply skipped: no login_uin")
            return

        max_depth = int(getattr(self.settings, "max_reply_depth", 1) or 1)
        state = self._load_auto_reply_state()
        fetch_limit = max(5, int(getattr(self.settings, "max_feed_limit", 20) or 20))
        payload = await self.controller.list_feeds(hostuin=login_uin, limit=fetch_limit, scope="self")
        entries = self._to_feed_entries(payload)
        replied = 0

        for entry in entries:
            if not entry.fid or not entry.hostuin:
                continue
            try:
                detail_payload = await self.controller.detail_feed(hostuin=entry.hostuin, fid=entry.fid, appid=entry.appid)
            except Exception:
                continue
            entry_data = detail_payload.get("entry")
            if isinstance(entry_data, dict):
                detail_entry = FeedEntry(**entry_data)
                if detail_entry.fid == entry.fid and detail_entry.hostuin == entry.hostuin:
                    entry = detail_entry
            post = post_from_entry(entry, detail=(detail_payload or {}).get("raw"), local_id=0)
            raw_comments = detail_payload.get("comments") or []
            post.comments = [
                QzoneComment(
                    commentid=str(item.get("commentid") or ""),
                    uin=int(item.get("uin") or 0),
                    nickname=str(item.get("nickname") or ""),
                    content=str(item.get("content") or ""),
                )
                for item in raw_comments
                if isinstance(item, dict)
            ]
            others_comments = [c for c in post.comments if c.uin != login_uin]
            if not others_comments:
                continue

            for comment in others_comments:
                comment_key = f"{entry.hostuin}:{entry.fid}:{comment.commentid}"
                current_depth = self._reply_depth_for_comment(state, comment_key)
                if max_depth > 0 and current_depth >= max_depth:
                    continue
                already_replied = any(
                    rc.uin == login_uin
                    and str(rc.content or "").strip()
                    for rc in post.comments
                    if rc.commentid != comment.commentid
                )
                if already_replied and current_depth > 0:
                    continue

                reply_text = await self._generate_reply_text(None, post, comment)
                if not reply_text.strip():
                    continue
                try:
                    await self.controller.reply_comment(
                        hostuin=entry.hostuin,
                        fid=entry.fid,
                        commentid=comment.commentid,
                        comment_uin=comment.uin,
                        content=reply_text.strip(),
                        appid=entry.appid,
                    )
                except Exception as exc:
                    logger.warning("qzone scheduled reply failed for %s: %s", comment_key, exc)
                    continue

                self._record_reply(state, comment_key, depth=current_depth + 1)
                self._save_auto_reply_state(state)
                replied += 1
                try:
                    await self._notify_admin_post_card(
                        None,
                        post,
                        f"定时自动回复了 {comment.nickname or '用户'} 的评论：{truncate(reply_text, 60)}",
                        comment_text=reply_text,
                    )
                except Exception:
                    pass
                break

        if replied:
            logger.info("qzone scheduled reply succeeded replied=%s", replied)

    def _get_cookie_lock(self) -> asyncio.Lock:
        if self._cookie_lock is None:
            self._cookie_lock = asyncio.Lock()
        return self._cookie_lock

    def _capture_onebot_client_from_context(self) -> Any | None:
        context = getattr(self, "_context", None) or getattr(self, "context", None)
        platform = None
        if context is not None:
            try:
                platform = context.get_platform("aiocqhttp")
            except Exception:
                platform = None
            if platform is None:
                try:
                    platform_manager = getattr(context, "platform_manager", None)
                    for candidate in getattr(platform_manager, "platform_insts", []):
                        meta = candidate.meta()
                        if getattr(meta, "name", "") == "aiocqhttp":
                            platform = candidate
                            break
                except Exception:
                    platform = None
        if platform is not None:
            bot = getattr(platform, "bot", None)
            if bot is not None:
                self._onebot_client = bot
                if hasattr(self, "_legacy_cfg"):
                    self._legacy_cfg.client = bot
        return self._onebot_client

    def _capture_onebot_client(self, event: AstrMessageEvent | None = None) -> Any | None:
        bot = getattr(event, "bot", None) if event is not None else None
        if bot is not None:
            self._onebot_client = bot
            if hasattr(self, "_legacy_cfg"):
                self._legacy_cfg.client = bot
            return bot
        return self._capture_onebot_client_from_context()

    def _cookie_binding_hint(self) -> str:
        return "没有从 AstrBot 拿到 aiocqhttp(OneBot v11) 客户端，请先用 qzone bind 手动绑定 Cookie。"

    async def _auto_bind_cookie(
        self,
        event: AstrMessageEvent | None = None,
        *,
        force: bool = False,
        source: str = "aiocqhttp",
    ) -> dict[str, Any]:
        async with self._get_cookie_lock():
            if not self.settings.auto_bind_cookie and not force:
                raise QzoneCookieAcquireError("自动绑定 Cookie 未开启")

            bot = self._capture_onebot_client(event)
            if bot is None:
                raise QzoneCookieAcquireError(f"无法从 OneBot 获取 Cookie。{self._cookie_binding_hint()}")

            try:
                status = await self.controller.get_status(probe_daemon=False)
            except QzoneBridgeError:
                status = {}

            if not force and status and int(status.get("cookie_count") or 0) > 0 and not bool(status.get("needs_rebind")):
                return status

            last_error: QzoneBridgeError | None = None
            for attempt in range(1, AUTO_BIND_RETRY_ATTEMPTS + 1):
                try:
                    cookie_text = await fetch_cookie_text(bot, domain=self.settings.cookie_domain)
                    if not cookie_text:
                        raise QzoneCookieAcquireError(f"OneBot 没有返回可用 Cookie。{self._cookie_binding_hint()}")

                    try:
                        cookie_uin = normalize_uin(parse_cookie_text(cookie_text))
                    except Exception:
                        cookie_uin = 0
                    payload = await self.controller.bind_cookie_local(cookie_text, uin=cookie_uin, source=source)
                    if attempt > 1:
                        logger.info("qzone auto bind succeeded on attempt %s/%s", attempt, AUTO_BIND_RETRY_ATTEMPTS)
                    return payload
                except QzoneBridgeError as exc:
                    last_error = exc
                except Exception as exc:
                    last_error = QzoneCookieAcquireError(f"自动绑定 Cookie 失败：{exc}")

                logger.warning(
                    "qzone auto bind attempt %s/%s failed: %s",
                    attempt,
                    AUTO_BIND_RETRY_ATTEMPTS,
                    last_error,
                )
                if attempt < AUTO_BIND_RETRY_ATTEMPTS:
                    await asyncio.sleep(AUTO_BIND_RETRY_DELAY_SECONDS)

            if last_error is not None:
                raise last_error
            raise QzoneCookieAcquireError(f"OneBot 没有返回可用 Cookie。{self._cookie_binding_hint()}")

    async def _ensure_cookie_ready(
        self,
        event: AstrMessageEvent | None = None,
        *,
        force: bool = False,
        source: str = "aiocqhttp",
    ) -> dict[str, Any] | None:
        try:
            status = await self.controller.get_status(probe_daemon=False)
        except QzoneBridgeError:
            status = {}
        if not force and status and int(status.get("cookie_count") or 0) > 0 and not bool(status.get("needs_rebind")):
            self._schedule_publish_render_asset_preload("cookie ready", event=event, status=status)
            return status
        payload = await self._auto_bind_cookie(event, force=force, source=source)
        self._schedule_publish_render_asset_preload("cookie bind", event=event, status=payload)
        return payload

    async def _bootstrap_auto_bind(self, trigger: str, event: AstrMessageEvent | None = None) -> bool:
        client = self._capture_onebot_client(event) if event is not None else self._capture_onebot_client_from_context()
        if client is None:
            await self._prewarm_daemon_if_cookie_ready(trigger)
            return False
        if not self.settings.auto_bind_cookie:
            await self._prewarm_daemon_if_cookie_ready(trigger)
            return True
        try:
            await self._ensure_cookie_ready(event, source="aiocqhttp")
        except QzoneBridgeError as exc:
            logger.warning("qzone auto bind on %s failed: %s", trigger, exc)
            return False
        await self._prewarm_daemon_if_cookie_ready(trigger)
        return True

    def _schedule_bootstrap_auto_bind(self, trigger: str, event: AstrMessageEvent | None = None) -> None:
        task = getattr(self, "_auto_bind_bootstrap_task", None)
        if task is not None and not task.done():
            return
        if bool(getattr(self, "_auto_bind_bootstrap_succeeded", False)):
            return
        if event is not None and self._capture_onebot_client(event) is None and self.settings.auto_bind_cookie:
            return

        async def runner() -> None:
            try:
                if await self._bootstrap_auto_bind(trigger, event):
                    self._auto_bind_bootstrap_succeeded = True
            except Exception:
                logger.warning("qzone auto bind on %s failed unexpectedly", trigger, exc_info=True)

        self._auto_bind_bootstrap_task = asyncio.create_task(runner())

    async def _prewarm_daemon_if_cookie_ready(self, trigger: str) -> None:
        if not self.settings.auto_start_daemon:
            return
        try:
            status = await self.controller.get_status(probe_daemon=False)
        except QzoneBridgeError:
            return
        if int(status.get("cookie_count") or 0) <= 0 or bool(status.get("needs_rebind")):
            return
        self._schedule_publish_render_asset_preload("daemon prewarm", status=status)
        self._schedule_daemon_warmup(trigger)

    def _schedule_daemon_warmup(self, trigger: str) -> None:
        if not self.settings.auto_start_daemon:
            return
        task = self._daemon_warmup_task
        if task is not None and not task.done():
            return

        async def runner() -> None:
            try:
                await self.controller.ensure_running()
            except QzoneBridgeError as exc:
                logger.warning("qzone daemon prewarm on %s failed: %s", trigger, exc)
            except Exception:
                logger.warning("qzone daemon prewarm on %s failed unexpectedly", trigger, exc_info=True)

        self._daemon_warmup_task = asyncio.create_task(runner())

    async def initialize(self):
        await self.db.initialize()

        if self.settings.cookies_str:
            try:
                payload = await self.controller.bind_cookie_local(self.settings.cookies_str, source="config")
                self._schedule_publish_render_asset_preload("config bind", status=payload)
            except QzoneBridgeError as exc:
                logger.warning("qzone config cookie bind failed: %s", exc)
        self._start_scheduled_tasks()
        self._schedule_bootstrap_auto_bind("initialize")

    @filter.on_astrbot_loaded()
    async def qzone_on_astrbot_loaded(self):
        self._start_scheduled_tasks()
        self._schedule_bootstrap_auto_bind("astrbot load")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def qzone_capture_aiocqhttp_client(self, event: AstrMessageEvent):
        self._capture_onebot_client(event)
        should_auto_read = self.settings.read_prob > 0 and random.random() < self.settings.read_prob
        should_auto_reply = self.settings.auto_reply_prob > 0 and random.random() < self.settings.auto_reply_prob
        if not should_auto_read and not should_auto_reply:
            self._schedule_bootstrap_auto_bind("aiocqhttp capture", event)
            return
        group_id = str(self._group_id(event) or "")
        sender_id = str(self._sender_id(event) or "")
        if group_id and group_id in self.settings.ignore_groups:
            self._schedule_bootstrap_auto_bind("aiocqhttp capture", event)
            return
        if sender_id and sender_id in self.settings.ignore_users:
            self._schedule_bootstrap_auto_bind("aiocqhttp capture", event)
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
        except Exception:
            self._schedule_bootstrap_auto_bind("aiocqhttp capture", event)
            return

        if should_auto_read:
            try:
                posts = await self._posts_for_event(
                    event,
                    ("看说说", "查看说说"),
                    target_id=int(sender_id or 0),
                    no_commented=True,
                    no_self=True,
                )
                if not posts:
                    return
                post = posts[0]
                content = await self._generate_comment_text(event, post)
                if not content.strip():
                    return
                await self._post_service().comment_post(post, content.strip())
                if self.settings.like_when_comment:
                    await self._post_service().like_post(post)
                if not self.settings.show_name:
                    await self._notify_admin_post_card(event, post, f"已自动评论 {self._post_display_nickname(post)} 的说说：{truncate(content, 60)}")
            except Exception as exc:
                logger.debug("qzone probabilistic read/comment failed: %s", exc)

        if should_auto_reply:
            try:
                await self._probabilistic_auto_reply(event)
            except Exception as exc:
                logger.debug("qzone probabilistic auto reply failed: %s", exc)

    async def _probabilistic_auto_reply(self, event: AstrMessageEvent) -> None:
        login_uin = 0
        try:
            status = await self.controller.get_status(probe_daemon=False)
            login_uin = int(status.get("login_uin") or 0)
        except Exception:
            pass
        if not login_uin:
            return

        max_depth = int(getattr(self.settings, "max_reply_depth", 1) or 1)
        state = self._load_auto_reply_state()
        fetch_limit = max(3, int(getattr(self.settings, "max_feed_limit", 20) or 20))
        payload = await self.controller.list_feeds(hostuin=login_uin, limit=fetch_limit, scope="self")
        entries = self._to_feed_entries(payload)

        for entry in entries:
            if not entry.fid or not entry.hostuin:
                continue
            try:
                detail_payload = await self.controller.detail_feed(hostuin=entry.hostuin, fid=entry.fid, appid=entry.appid)
            except Exception:
                continue
            entry_data = detail_payload.get("entry")
            if isinstance(entry_data, dict):
                detail_entry = FeedEntry(**entry_data)
                if detail_entry.fid == entry.fid and detail_entry.hostuin == entry.hostuin:
                    entry = detail_entry
            post = post_from_entry(entry, detail=(detail_payload or {}).get("raw"), local_id=0)
            raw_comments = detail_payload.get("comments") or []
            post.comments = [
                QzoneComment(
                    commentid=str(item.get("commentid") or ""),
                    uin=int(item.get("uin") or 0),
                    nickname=str(item.get("nickname") or ""),
                    content=str(item.get("content") or ""),
                )
                for item in raw_comments
                if isinstance(item, dict)
            ]
            others_comments = [c for c in post.comments if c.uin != login_uin]
            if not others_comments:
                continue

            for comment in others_comments:
                comment_key = f"{entry.hostuin}:{entry.fid}:{comment.commentid}"
                current_depth = self._reply_depth_for_comment(state, comment_key)
                if max_depth > 0 and current_depth >= max_depth:
                    continue

                reply_text = await self._generate_reply_text(event, post, comment)
                if not reply_text.strip():
                    continue
                try:
                    await self.controller.reply_comment(
                        hostuin=entry.hostuin,
                        fid=entry.fid,
                        commentid=comment.commentid,
                        comment_uin=comment.uin,
                        content=reply_text.strip(),
                        appid=entry.appid,
                    )
                except Exception as exc:
                    logger.warning("qzone probabilistic reply failed for %s: %s", comment_key, exc)
                    continue

                self._record_reply(state, comment_key, depth=current_depth + 1)
                self._save_auto_reply_state(state)
                try:
                    await self._notify_admin_post_card(
                        event,
                        post,
                        f"概率触发回复了 {comment.nickname or '用户'} 的评论：{truncate(reply_text, 60)}",
                        comment_text=reply_text,
                    )
                except Exception:
                    pass
                return

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("访客", alias={"查看访客"})
    async def view_visitor(self, event: AstrMessageEvent):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            payload = await self.controller.view_visitors()
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._markdown_result(event, self._format_visitors(payload), subdir="visitors")

    @filter.command("看说说", alias={"查看说说"})
    async def view_feed(self, event: AstrMessageEvent):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            posts = await self._posts_for_event(event, ("看说说", "查看说说"), with_detail=True)
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        async for result in self._yield_post_card_results(event, posts, self._format_posts(posts, detail=True)):
            yield result

    @filter.command("评说说", alias={"评论说说", "读说说"})
    async def comment_feed(self, event: AstrMessageEvent):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            selection = self._selection_for_event(event, ("评说说", "评论说说", "读说说"))
            use_safety_filters = not _selection_has_explicit_input(selection)
            posts = await self._posts_for_selection(
                selection,
                with_detail=True,
                no_commented=use_safety_filters,
                no_self=use_safety_filters,
                login_uin=self._self_id(event),
            )
            if not posts:
                yield self._command_result(event, "没有找到可评论的说说。可以先用 看说说 1~3 确认编号或范围。")
                return
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return

        lines: list[str] = []
        error_lines: list[str] = []
        comment_texts: dict[int, str] = {}
        commented_posts: list[QzonePost] = []
        for post in posts:
            content = selection.comment_text or await self._generate_comment_text(event, post)
            if not content.strip():
                content = "挺有意思的。"
            try:
                await self._post_service().comment_post(post, content)
            except QzoneBridgeError as exc:
                error_lines.append(f"第 {post.local_id} 条评论失败：{self._error_text(exc)}")
                continue
            if self.settings.like_when_comment:
                try:
                    await self._post_service().like_post(post)
                except QzoneBridgeError as exc:
                    error_lines.append(f"第 {post.local_id} 条已评论，但点赞失败：{self._error_text(exc)}")
            comment_texts[id(post)] = content
            commented_posts.append(post)
            lines.append(f"已评论第 {post.local_id} 条：{truncate(content, 60)}")

        if not self.settings.show_name:
            async for result in self._yield_post_card_results(
                event,
                commented_posts,
                self._format_posts(commented_posts, detail=True),
                fallback_when_unrendered=False,
                comment_texts=comment_texts,
            ):
                yield result
        yield self._command_result(event, "\n".join([*lines, *error_lines]))

    @filter.command("点赞说说", alias={"赞说说"})
    async def like_feed(self, event: AstrMessageEvent):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            posts = await self._posts_for_event(event, ("点赞说说", "赞说说"), with_detail=True)
            if not posts:
                yield self._command_result(event, "没有找到可点赞的说说。")
                return
            lines: list[str] = []
            for post in posts:
                payload = await self._post_service().like_post(post)
                lines.append(format_like_result(payload))
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield self._command_result(event, "\n".join(lines))
        async for result in self._yield_post_card_results(
            event,
            posts,
            self._format_posts(posts, detail=True),
            fallback_when_unrendered=False,
        ):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发说说")
    async def publish_feed(self, event: AstrMessageEvent, content: str = ""):
        self._stop_event(event)
        post = collect_post_payload(
            event,
            fallback_content=content,
            include_event_text=True,
            command_prefixes=("发说说",),
        )
        profile_task: asyncio.Task | None = None
        try:
            await self._ensure_cookie_ready(event)
            profile_task = self._schedule_publisher_profile(event)
            payload = await self.controller.publish_post(
                content=post.content,
                media=[item.to_dict() for item in post.media],
                content_sanitized=True,
            )
            if payload.get("fid"):
                await self._post_store().upsert_async(
                    QzonePost(
                        hostuin=self._self_id(event),
                        fid=str(payload.get("fid") or ""),
                        appid=311,
                        summary=post.content,
                        images=[str(item.source) for item in post.media],
                    )
                )
        except QzoneBridgeError as exc:
            if profile_task is not None:
                profile_task.cancel()
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._publish_result(event, post, payload, profile_task=profile_task)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删说说")
    async def delete_feed(self, event: AstrMessageEvent):
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            posts = await self._posts_for_event(
                event,
                ("删说说",),
                target_id=self._self_id(event),
            )
            if not posts:
                yield self._command_result(event, "没有找到可删除的说说。")
                return
            lines: list[str] = []
            for post in posts:
                payload = await self._post_service().delete_post(post)
                lines.append(format_action_result("删除结果", payload))
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._markdown_result(event, "\n".join(lines), subdir="posts")

    @filter.command("回复评论", alias={"回评"})
    async def reply_comment(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield self._command_result(event, "只有管理员可以回复评论。")
            return
        raw = self._message_after_command(self._event_text(event), ("回复评论", "回评"))
        parts = raw.split()
        post_id = int(parts[0]) if parts and re.fullmatch(r"-?\d+", parts[0]) else 0
        if post_id <= 0:
            yield self._command_result(event, "请提供要回评的稿件ID，例如：回复评论 3。")
            return
        comment_position = 0
        if len(parts) > 1:
            if not re.fullmatch(r"\d+", parts[1]):
                yield self._command_result(event, "评论序号需要是从 1 开始的数字。")
                return
            comment_position = int(parts[1])
            if comment_position <= 0:
                yield self._command_result(event, "评论序号需要从 1 开始。")
                return
        saved = await self._post_store().get_async(post_id)
        if saved is None:
            yield self._command_result(event, f"稿件 #{post_id} 不存在或还没有发布。")
            return
        try:
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            status = await self.controller.get_status(probe_daemon=False)
            hostuin = saved.hostuin
            fid = saved.fid
            appid = saved.appid
            detail = await self.controller.detail_feed(hostuin=hostuin, fid=fid, appid=appid)
            entry = FeedEntry(**detail["entry"])
            post = post_from_entry(entry, detail=detail.get("raw"), local_id=post_id)
            if detail.get("comments"):
                post.comments = [
                    QzoneComment(
                        commentid=str(item.get("commentid") or ""),
                        uin=int(item.get("uin") or 0),
                        nickname=str(item.get("nickname") or ""),
                        content=str(item.get("content") or ""),
                    )
                    for item in detail.get("comments") or []
                    if isinstance(item, dict)
                ]
            await self._post_store().upsert_async(post)
            login_uin = int(status.get("login_uin") or 0)
            comments = [item for item in post.comments if not login_uin or item.uin != login_uin]
            if not comments:
                yield self._command_result(event, "这条说说暂时没有可回复的评论。")
                return
            if comment_position > len(comments):
                yield self._command_result(event, f"这条说说只有 {len(comments)} 条可回复评论。")
                return
            comment = comments[comment_position - 1] if comment_position else comments[-1]
            content = await self._generate_reply_text(event, post, comment)
            if not content.strip():
                content = "收到啦。"
            payload = await self.controller.reply_comment(
                hostuin=hostuin,
                fid=fid,
                commentid=comment.commentid,
                comment_uin=comment.uin,
                content=content.strip(),
                appid=post.appid,
            )
        except QzoneBridgeError as exc:
            yield self._command_result(event, self._error_text(exc))
            return
        yield await self._markdown_result(event, format_action_result("回复结果", payload), subdir="posts")

    @filter.command("投稿")
    async def contribute_post(self, event: AstrMessageEvent):
        async for msg in self.campus_wall.contribute(event):
            yield msg

    @filter.command("匿名投稿")
    async def anon_contribute_post(self, event: AstrMessageEvent):
        async for msg in self.campus_wall.contribute(event, anon=True):
            yield msg

    @filter.command("撤稿")
    async def recall_post(self, event: AstrMessageEvent):
        async for msg in self.campus_wall.delete(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看稿件", alias={"看稿"})
    async def view_post(self, event: AstrMessageEvent):
        async for msg in self.campus_wall.view(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("通过稿件", alias={"过稿", "通过投稿"})
    async def approve_post(self, event: AstrMessageEvent):
        async for msg in self.campus_wall.approve(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拒绝稿件", alias={"拒稿", "拒绝投稿"})
    async def reject_post(self, event: AstrMessageEvent):
        async for msg in self.campus_wall.reject(event):
            yield msg

    @filter.command("qzone bind")
    async def qzone_bind(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield self._command_result(event, "只有管理员可以绑定 Cookie。")
            return
        raw = self._message_after_command(self._event_text(event), ("qzone bind",))
        if raw.strip():
            try:
                payload = await self.controller.bind_cookie_local(raw.strip())
            except QzoneBridgeError as exc:
                logger.warning("qzone bind failed: %s", exc)
                yield self._command_result(event, self._error_text(exc))
                return
            self._schedule_publish_render_asset_preload("manual bind", event=event, status=payload)
            self._schedule_daemon_warmup("manual bind")
            yield self._command_result(event, format_status(payload))
            return
        try:
            payload = await self._auto_bind_cookie(event, force=True, source="aiocqhttp")
        except QzoneBridgeError as exc:
            logger.warning("qzone autobind failed: %s", exc)
            yield self._command_result(event, self._error_text(exc))
            return
        self._schedule_publish_render_asset_preload("autobind", event=event, status=payload)
        self._schedule_daemon_warmup("autobind")
        yield self._command_result(event, format_status(payload))

    @filter.llm_tool()
    async def llm_view_feed(
        self,
        event: AstrMessageEvent,
        user_id: str | None = None,
        pos: int = 0,
        like: bool = False,
        reply: bool = False,
    ):
        """查看、点赞、评论某位用户QQ空间的某条说说、动态
        Args:
            user_id(string): 目标用户的QQ账号，必定为一串数字，如(12345678), 默认为当前用户QQ号
            pos(number): 要查询的说说序号, 默认为0表示最新
            like(boolean): 是否点赞
            reply(boolean): 是否评论
        """
        try:
            user_id = user_id or str(self._sender_id(event))
            await self._ensure_cookie_ready(event)
            await self._ensure_daemon()
            hostuin = int(user_id) if str(user_id).isdigit() else self._sender_id(event)
            selection = PostSelection(
                target_uin=hostuin,
                start=(pos + 1 if pos >= 0 else -1),
                end=(pos + 1 if pos >= 0 else -1),
                selector="index" if pos >= 0 else "last",
            )
            posts = await self._posts_for_selection(selection, with_detail=True, login_uin=self._self_id(event))
            if not posts:
                return "查询结果为空"
            post = posts[0]
            msg = ""
            if like and reply:
                content = await self._generate_comment_text(event, post)
                if content.strip():
                    await self._post_service().comment_post(post, content.strip())
                await self._post_service().like_post(post)
                msg = "已评论并点赞"
            elif reply:
                content = await self._generate_comment_text(event, post)
                if content.strip():
                    await self._post_service().comment_post(post, content.strip())
                msg = "已评论"
            elif like:
                await self._post_service().like_post(post)
                msg = "已点赞"
            return msg + "\n" + post.summary + "\n" + "\n".join(post.images[:9])
        except Exception as e:
            logger.error(f"LLM查看说说失败: {e}")
            return "查看说说失败，请稍后重试"

    @filter.llm_tool()
    async def llm_publish_feed(
        self,
        event: AstrMessageEvent,
        text: str = "",
        get_image: bool = True,
    ):
        """写一篇说说并发布到QQ空间
        Args:
            text(string): 要发布的说说内容
            get_image(boolean): 是否获取当前对话中的图片附加到说说里, 默认为True
        """
        images = await get_image_urls(event) if get_image else []
        media = normalize_media_list(images, trusted_local=True)
        try:
            payload = await self.controller.publish_post(
                content=text,
                media=[item.to_dict() for item in media],
                content_sanitized=True,
            )
            if not self.settings.show_name:
                post = PostPayload(content=text, media=media)
                await self._notify_admin_publish_result(post, payload, "已发布说说")
            img_count = len(media)
            return (
                f"已发布说说到QQ空间。"
                + (f" 配图 {img_count} 张。" if img_count else "")
            )
        except Exception as e:
            logger.error(f"LLM发布说说失败: {e}")
            return "发布说说失败，请稍后重试"

    async def terminate(self):
        for task in self._scheduled_tasks:
            task.cancel()
        self._scheduled_tasks.clear()
        try:
            await self.controller.close()
        except Exception:
            pass
        if self._legacy_cfg.cache_dir.exists():
            try:
                shutil.rmtree(self._legacy_cfg.cache_dir)
            except Exception as e:
                logger.error(f"清理缓存失败: {e}")

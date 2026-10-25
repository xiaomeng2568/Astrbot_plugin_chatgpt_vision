"""QQ image understanding, independent from existing image generation/captioning."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from collections import OrderedDict
from pathlib import Path

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .vision.client import VisionClient
from .vision.errors import VisionError
from .vision.images import ImageReader
from .vision.network import RoutedSession
from .vision.oauth import OAuth
from .vision.storage import Store


def image_references(event, include_replies=False) -> list[str]:
    refs = []
    for component in event.get_messages():
        if isinstance(component, Comp.Image):
            value = component.url or component.file
            if value:
                refs.append(str(value))
        elif include_replies and isinstance(component, Comp.Reply):
            for child in component.chain or []:
                if isinstance(child, Comp.Image):
                    value = child.url or child.file
                    if value:
                        refs.append(str(value))
    return list(dict.fromkeys(refs))


class ChatGPTVisionPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context, self.config = context, config
        self.data_dir = Path(StarTools.get_data_dir())
        self.store = Store(self.data_dir)
        self.session = None
        self.client = None
        self.busy = asyncio.Lock()
        self.seen = OrderedDict()
        roots = [Path(get_astrbot_temp_path())]
        roots.extend(Path(value) for value in config.get("trusted_image_dirs", []) if str(value).strip())
        self.reader = ImageReader(roots, config.get("max_image_edge", 2048), config.get("image_proxy", ""))

    def get_client(self) -> VisionClient:
        if self.session is None or self.session.closed:
            self.session = RoutedSession(self.config.get("openai_proxy", ""))
            self.client = VisionClient(OAuth(self.store, self.session), int(self.config.get("request_timeout", 90)))
        return self.client

    def _platform_allowed(self, event) -> bool:
        return event.get_platform_name() in self.config.get("qq_platforms", ["aiocqhttp", "qq_official", "qq_official_webhook"])

    def _authorized(self, event) -> bool:
        group = str(event.get_group_id() or "")
        if group and group not in {str(v) for v in self.config.get("allowed_group_ids", [])}:
            return False
        return event.is_admin() or str(event.get_sender_id()) in {str(v) for v in self.config.get("allowed_user_ids", [])}

    def _claim(self, event) -> bool:
        message_id = str(getattr(event.message_obj, "message_id", "") or id(event))
        key = hashlib.sha256((str(event.unified_msg_origin) + "\n" + message_id).encode()).hexdigest()
        now = time.monotonic()
        while self.seen and next(iter(self.seen.values())) < now - 180:
            self.seen.popitem(last=False)
        if key in self.seen or event.get_extra("chatgpt_vision_handled", False):
            event.stop_event()
            return False
        self.seen[key] = now
        while len(self.seen) > 256:
            self.seen.popitem(last=False)
        event.set_extra("chatgpt_vision_handled", True)
        event.stop_event()
        return True

    async def _persona(self, event) -> str:
        if not self.config.get("inherit_persona", True):
            return ""
        try:
            manager = self.context.persona_manager
            conversations = self.context.conversation_manager
            cid = await conversations.get_curr_conversation_id(event.unified_msg_origin)
            conversation = await conversations.get_conversation(event.unified_msg_origin, cid) if cid else None
            persona_id = getattr(conversation, "persona_id", None)
            if persona_id:
                persona = manager.get_persona(persona_id)
                if inspect.isawaitable(persona):
                    persona = await persona
                if persona is not None:
                    return str(persona.system_prompt)
            default = manager.get_default_persona_v3(umo=event.unified_msg_origin)
            if inspect.isawaitable(default):
                default = await default
            return str(default.get("prompt", ""))
        except Exception:
            # Never log persona content or attach it to diagnostics.
            return ""

    async def _handle(self, event, refs, prompt):
        if not self._claim(event):
            return
        try:
            if not self._authorized(event):
                await event.send(event.plain_result("这个识图账号还没允许当前用户或群使用，请联系管理员。"))
                return
            if not refs:
                await event.send(event.plain_result("唔，请带上一张图片，或引用图片后再说识图。"))
                return
            if len(refs) > 3:
                raise VisionError("image_limit")
            if self.busy.locked():
                raise VisionError("busy")
            async with self.busy:
                # Validate account/model before downloading private input.
                state = self.store.load()
                account = self.store.active(state)
                selected = account.get("selected_model", "")
                if not selected or time.time() - account.get("verified_models", {}).get(selected, 0) > 86400:
                    raise VisionError("model")
                data_urls = []
                total = 0
                for ref in refs:
                    image, size = await self.reader.read(ref)
                    total += size
                    if total > 16 * 1024 * 1024:
                        raise VisionError("image_limit")
                    data_urls.append(image)
                result = await self.get_client().describe(prompt, data_urls, await self._persona(event),
                                                          self.config.get("max_reply_chars", 500))
            await event.send(event.plain_result(result))
        except asyncio.CancelledError:
            raise
        except VisionError as exc:
            logger.warning(f"[ChatGPTVision] request stopped ({exc.code})")
            await event.send(event.plain_result(str(exc)))
        except Exception:
            logger.warning("[ChatGPTVision] request stopped (internal)")
            await event.send(event.plain_result(str(VisionError("internal"))))

    @filter.command("OAuth识图", priority=1000000)
    async def describe_command(self, event: AstrMessageEvent):
        if not self._platform_allowed(event):
            return
        if self.config.get("backend", "existing") != "chatgpt_oauth":
            event.stop_event()
            await event.send(event.plain_result("当前沿用原有图片转述流程。请按原来的方式发图，或让管理员选择 OAuth 识图。"))
            return
        prompt = event.get_message_str().strip()
        for prefix in ("/OAuth识图", "OAuth识图"):
            if prompt.startswith(prefix):
                prompt = prompt[len(prefix):].strip()
                break
        await self._handle(event, image_references(event, self.config.get("include_replied_images", False)), prompt)

    @filter.on_waiting_llm_request(priority=1000000)
    async def automatic_vision(self, event: AstrMessageEvent):
        if (self.config.get("backend", "existing") != "chatgpt_oauth" or
                not self.config.get("auto_reply", False) or not self._platform_allowed(event)):
            return
        refs = image_references(event, False)
        if refs:
            await self._handle(event, refs, event.get_message_str())

    @filter.command("OAuth识图状态", priority=1000000)
    async def vision_status(self, event: AstrMessageEvent):
        if not event.is_admin():
            event.stop_event()
            return
        event.stop_event()
        try:
            state = self.store.load()
            account = state.get("accounts", {}).get(state.get("active"), {})
            logged = bool(account.get("access_token"))
            permission = {"resource.invoke", "chatgpt.tokens.use.direct"}.issubset(set(account.get("scopes", [])))
            selected = account.get("selected_model", "")
            verified = bool(selected) and time.time() - account.get("verified_models", {}).get(selected, 0) <= 86400
            message = ("当前后端：" + ("OAuth识图" if self.config.get("backend") == "chatgpt_oauth" else "原有转述流程") +
                       "。已登录：" + ("是" if logged else "否") + "。推理授权：" + ("有" if permission else "无") +
                       "。视觉验证：" + ("通过" if verified else "未确认") + "。")
        except Exception:
            message = str(VisionError("storage"))
        await event.send(event.plain_result(message))

    async def terminate(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.seen.clear()

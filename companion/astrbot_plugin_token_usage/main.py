"""Usage statistics without additional model calls or content retention."""
import asyncio
import inspect
import time

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.star_tools import StarTools

from .meter.card import render
from .meter.observer import Observer, scope_context
from .meter.report import period_start, plain
from .meter.store import Store
from .meter.usage import parse_usage, safe_name


class TokenUsagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context, self.config = context, config
        self.store = Store(StarTools.get_data_dir(), config.get('retention_days', 90), config.get('max_records', 100000))
        self.queue = asyncio.Queue(maxsize=1024)
        self.worker = None
        self.closed = False
        self.dropped = 0
        self.observer = Observer(self._enqueue)

    def _ensure_worker(self):
        if not self.closed and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self._writer())

    async def initialize(self):
        self._ensure_worker()
        self._refresh()

    def _refresh(self):
        if not self.closed and self.config.get('enabled', True):
            try:
                self.observer.install(self.context.get_all_providers())
            except Exception:
                logger.warning('[TokenUsage] provider observation unavailable')

    async def _enqueue(self, data):
        if self.closed or not self.config.get('enabled', True):
            return
        self._ensure_worker()
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _writer(self):
        while True:
            data = await self.queue.get()
            try:
                await asyncio.to_thread(self.store.add, **data, prices=self.config.get('prices', []))
            except Exception:
                self.dropped += 1
                logger.warning('[TokenUsage] usage record unavailable')
            finally:
                self.queue.task_done()

    async def _conversation(self, event):
        try:
            return await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin) or ''
        except Exception:
            return ''

    @filter.on_llm_request(priority=-1000000)
    async def bind_request(self, event, request):
        self._refresh()
        cid = getattr(getattr(request, 'conversation', None), 'cid', '') or await self._conversation(event)
        scope_context.set(dict(scope=str(event.unified_msg_origin), conversation=str(cid)))

    @filter.on_agent_done(priority=-1000000)
    async def clear_request(self, event, run_context, response):
        scope_context.set(None)

    async def ingest_external(self, event, *, request_id, model, usage, status='completed', **_ignored):
        """Public bridge for ChatGPT OAuth vision. Only scalar counters are retained."""
        await self._enqueue(dict(request_id='oauth:' + str(request_id), scope=str(event.unified_msg_origin),
                                 conversation=str(await self._conversation(event)), provider='ChatGPT OAuth',
                                 model=safe_name(model), usage=parse_usage(usage), source='chatgpt_oauth',
                                 billing='subscription', status=status))

    @filter.command('token', priority=1000000)
    async def token_command(self, event: AstrMessageEvent):
        event.stop_event()
        if self.config.get('query_access', 'admins') == 'admins' and not event.is_admin():
            await event.send(event.plain_result('用量统计仅管理员可查看。'))
            return
        text = event.get_message_str().strip()
        for prefix in ('/token', 'token'):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        args = text.split()
        period, global_scope, model, text_only = '今日', False, None, False
        aliases = {'today': '今日', 'week': '近7天', 'all': '累计', 'session': '当前会话', '近七天': '近7天'}
        if args and args[0] in {'帮助', 'help'}:
            await event.send(event.plain_result('/token [今日/近7天/累计/当前会话] [全部] [模型 名称] [文本]\n全部仅管理员可查。'))
            return
        index = 0
        while index < len(args):
            word = aliases.get(args[index], args[index])
            if word in {'今日', '近7天', '累计', '当前会话'}:
                period = word
            elif word == '全部':
                global_scope = True
            elif word == '文本':
                text_only = True
            elif word == '模型' and index + 1 < len(args):
                index += 1
                model = safe_name(args[index])
            else:
                await event.send(event.plain_result('参数不认识，发送 /token 帮助 查看用法。'))
                return
            index += 1
        if global_scope and not event.is_admin():
            await event.send(event.plain_result('只有管理员可以查看全部会话用量。'))
            return
        if global_scope and period == '当前会话':
            await event.send(event.plain_result('当前会话不能同时选择全部。'))
            return
        scope = None if global_scope else str(event.unified_msg_origin)
        cid = await self._conversation(event) if period == '当前会话' else None
        if period == '当前会话' and not cid:
            await event.send(event.plain_result('当前会话ID不可用，可先查询今日或累计。'))
            return
        try:
            self._ensure_worker()
            await asyncio.wait_for(self.queue.join(), timeout=10)
            report = await asyncio.to_thread(self.store.report, since=period_start(period), scope=scope,
                                             conversation=cid, model=model)
            label = '全部会话' if global_scope else '当前会话来源'
            dropped = self.dropped + self.observer.errors
            if self.config.get('card', True) and not text_only and report['calls'] and not dropped:
                try:
                    image = await asyncio.to_thread(render, report, period, label)
                except Exception:
                    image = None
                if image is not None:
                    await event.send(event.chain_result([Comp.Image.fromBytes(image)]))
                    return
            await event.send(event.plain_result(plain(report, period, label, dropped)))
        except Exception:
            logger.warning('[TokenUsage] report unavailable')
            await event.send(event.plain_result('统计暂时不可用，请稍后重试。'))

    async def terminate(self):
        self.closed = True
        self.observer.restore()
        if self.worker:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=10)
            except asyncio.TimeoutError:
                pass
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass

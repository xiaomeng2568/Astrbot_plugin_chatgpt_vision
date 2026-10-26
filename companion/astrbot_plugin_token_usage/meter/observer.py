"""Reversible per-instance provider observation; no HTTP hooks or request capture."""
import contextvars
import inspect
import time
import uuid
from functools import wraps

from .usage import Usage, field, response_model, response_usage, safe_name

scope_context = contextvars.ContextVar('token_usage_scope', default=None)
active_call = contextvars.ContextVar('token_usage_nested', default=False)


class Observer:
    def __init__(self, emit):
        self.emit = emit
        self.patches = []
        self.errors = 0
        self.closed = False

    def install(self, providers):
        for provider in providers:
            for name in ('text_chat', 'text_chat_stream'):
                if any(p[0] is provider and p[1] == name for p in self.patches):
                    continue
                original = getattr(provider, name, None)
                if not callable(original):
                    continue
                own = name in getattr(provider, '__dict__', {})
                wrapper = self._stream(provider, original) if name.endswith('_stream') else self._plain(provider, original)
                try:
                    setattr(provider, name, wrapper)
                    self.patches.append((provider, name, original, wrapper, own))
                except (AttributeError, TypeError):
                    self.errors += 1

    async def _record(self, provider, response, fallback, status, scope, request_id):
        try:
            meta = provider.meta()
            await self.emit(dict(request_id=request_id, scope=(scope or {}).get('scope', ''),
                                 conversation=(scope or {}).get('conversation', ''),
                                 provider=safe_name(field(meta, 'id')),
                                 model=response_model(response, fallback), usage=response_usage(response),
                                 status=status, source='provider', billing='unknown'))
        except Exception:
            self.errors += 1

    def _plain(self, provider, original):
        @wraps(original)
        async def wrapped(*args, **kwargs):
            if self.closed or active_call.get():
                return await original(*args, **kwargs)
            token = active_call.set(True)
            scope = scope_context.get()
            result = None
            status = 'interrupted'
            request_id = uuid.uuid4().hex
            try:
                model = safe_name(kwargs.get('model') or provider.get_model())
            except Exception:
                model = '未知'
            try:
                result = await original(*args, **kwargs)
                status = 'failed' if field(result, 'role') == 'err' else 'completed'
                return result
            except Exception:
                status = 'failed'
                raise
            finally:
                active_call.reset(token)
                await self._record(provider, result, model, status, scope, request_id)
        return wrapped

    def _stream(self, provider, original):
        @wraps(original)
        async def wrapped(*args, **kwargs):
            if self.closed or active_call.get():
                stream = original(*args, **kwargs)
                if inspect.isawaitable(stream):
                    stream = await stream
                try:
                    async for part in stream:
                        yield part
                finally:
                    if hasattr(stream, 'aclose'):
                        await stream.aclose()
                return
            # Reset before each yield: ContextVars must not leak into the consumer
            # task, which may interleave multiple provider generators.
            scope, request_id = scope_context.get(), uuid.uuid4().hex
            result, status, stream, last_role = None, 'interrupted', None, None
            try:
                model = safe_name(kwargs.get('model') or provider.get_model())
            except Exception:
                model = '未知'
            try:
                token = active_call.set(True)
                try:
                    stream = original(*args, **kwargs)
                    if inspect.isawaitable(stream):
                        stream = await stream
                finally:
                    active_call.reset(token)
                while True:
                    token = active_call.set(True)
                    try:
                        part = await anext(stream)
                    except StopAsyncIteration:
                        status = 'failed' if last_role == 'err' else 'completed'
                        break
                    finally:
                        active_call.reset(token)
                    usage = response_usage(part)
                    last_role = field(part, 'role')
                    # Last aggregate report wins; never add cumulative stream chunks.
                    if usage.input is not None or usage.output is not None:
                        result = part
                    elif result is None:
                        result = part
                    yield part
            except Exception:
                status = 'failed'
                raise
            finally:
                try:
                    if stream is not None and hasattr(stream, 'aclose'):
                        await stream.aclose()
                finally:
                    await self._record(provider, result, model, status, scope, request_id)
        return wrapped

    def restore(self):
        self.closed = True
        for provider, name, original, wrapper, own in reversed(self.patches):
            if getattr(provider, name, None) is wrapper:
                if own:
                    setattr(provider, name, original)
                else:
                    delattr(provider, name)
        self.patches.clear()

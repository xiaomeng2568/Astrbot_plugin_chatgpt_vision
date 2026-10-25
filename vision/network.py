"""Separate application proxy routes; system proxy discovery without netrc auth."""
from __future__ import annotations

from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

import aiohttp

from .errors import VisionError


def validate_proxy(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.path not in {"", "/"} or parsed.query or parsed.fragment or
                (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError()
        return value
    except Exception:
        raise VisionError("network") from None


def choose_proxy(url: str, configured="") -> str | None:
    if configured:
        return validate_proxy(str(configured).strip())
    try:
        parsed = urlsplit(str(url))
        if proxy_bypass(parsed.hostname or ""):
            return None
        proxies = getproxies()
        value = proxies.get(parsed.scheme) or proxies.get("all")
        return validate_proxy(str(value).strip()) if value else None
    except VisionError:
        raise
    except Exception:
        raise VisionError("network") from None


class RoutedSession:
    def __init__(self, proxy="", **kwargs):
        self.proxy = validate_proxy(str(proxy or "").strip())
        self.last_route = None
        # Explicit discovery avoids aiohttp trust_env also loading unrelated .netrc logins.
        self.raw = aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), **kwargs)

    @property
    def closed(self):
        return self.raw.closed

    def request(self, method, url, **kwargs):
        routing_url = kwargs.pop("routing_url", url)
        kwargs["proxy"] = choose_proxy(str(routing_url), self.proxy)
        self.last_route = "custom_proxy" if self.proxy else ("system_proxy" if kwargs["proxy"] else "direct")
        return self.raw.request(method, url, **kwargs)

    async def close(self):
        await self.raw.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

"""Memory-only image ingestion with bounded decoding and SSRF-safe resolution."""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import socket
import warnings
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from PIL import Image, ImageOps
from yarl import URL

from .errors import VisionError
from .network import RoutedSession

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000


def public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        return address.is_global and not address.is_multicast and not address.is_reserved
    except ValueError:
        return False


def check_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or
                parsed.password or parsed.fragment or parsed.port not in {None, 80, 443}):
            raise ValueError()
        host = parsed.hostname
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local")):
                raise ValueError()
        else:
            if not public_ip(host):
                raise ValueError()
    except Exception:
        raise VisionError("image_source") from None


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
            if not answers or any(not public_ip(item[4][0]) for item in answers):
                raise VisionError("image_source")
            return [{"hostname": host, "host": item[4][0], "port": port, "family": item[0],
                     "proto": item[2], "flags": socket.AI_NUMERICHOST} for item in answers]
        except VisionError:
            raise
        except Exception:
            raise VisionError("image_source") from None

    async def close(self):
        pass


def normalize_image(data: bytes, max_edge=2048) -> str:
    if not data or len(data) > MAX_BYTES:
        raise VisionError("image_limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise VisionError("image")
                if source.width * source.height > MAX_PIXELS:
                    raise VisionError("image_limit")
                source.verify()
            with Image.open(BytesIO(data)) as source:
                if getattr(source, "n_frames", 1) > 1:
                    # Never silently analyze an arbitrary frame of an animation.
                    raise VisionError("image")
                source = ImageOps.exif_transpose(source)
                source.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                rgb = source.convert("RGB")
                clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())
                output = BytesIO()
                clean.save(output, format="PNG")
        result = output.getvalue()
        if len(result) > MAX_BYTES:
            raise VisionError("image_limit")
        return "data:image/png;base64," + base64.b64encode(result).decode("ascii")
    except VisionError:
        raise
    except Exception:
        raise VisionError("image") from None


def read_local(value: str, allowed_roots: list[Path]) -> bytes:
    try:
        if value.lower().startswith("file:"):
            parsed = urlsplit(value)
            if parsed.netloc not in {"", "localhost"}:
                raise ValueError()
            value = unquote(parsed.path)
            if os.name == "nt" and len(value) > 2 and value[0] == "/" and value[2] == ":":
                value = value[1:]
        if value.startswith(("\\\\", "//")):
            raise ValueError()
        path = Path(value)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError()
        path = path.resolve(strict=True)
        if not any(path.is_relative_to(root.resolve()) for root in allowed_roots):
            raise ValueError()
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError()
        with path.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise VisionError("image_limit")
        return data
    except VisionError:
        raise
    except Exception:
        raise VisionError("image_source") from None


class ImageReader:
    def __init__(self, roots: list[Path], max_edge=2048, proxy=""):
        self.roots = roots
        self.max_edge = max(512, min(4096, int(max_edge)))
        self.proxy = proxy

    async def read(self, value: str) -> tuple[str, int]:
        try:
            if not isinstance(value, str) or len(value) > MAX_BYTES * 4 // 3 + 100:
                raise VisionError("image_limit")
            if value.startswith(("base64://", "data:image/")):
                payload = value[9:] if value.startswith("base64://") else value.partition(",")[2]
                data = base64.b64decode(payload, validate=True)
            elif value.startswith(("https://", "http://")):
                data = await self.download(value)
            else:
                data = await asyncio.to_thread(read_local, value, self.roots)
            image = await asyncio.to_thread(normalize_image, data, self.max_edge)
            return image, len(data)
        except VisionError:
            raise
        except Exception:
            raise VisionError("image") from None

    async def download(self, url: str) -> bytes:
        async with RoutedSession(self.proxy, timeout=aiohttp.ClientTimeout(total=25, sock_read=10)) as session:
            for _ in range(4):
                check_url(url)
                parsed = urlsplit(url)
                # Pin the verified public destination even when an HTTP proxy resolves targets.
                # Host and TLS SNI remain the original CDN host; no signed URL goes to OpenAI.
                answers = await asyncio.wait_for(PublicResolver().resolve(parsed.hostname, parsed.port or
                                                        (443 if parsed.scheme == "https" else 80)), timeout=10)
                target = URL(url, encoded=True).with_host(answers[0]["host"])
                options = {"allow_redirects": False, "headers": {"Host": parsed.netloc}, "routing_url": url}
                if parsed.scheme == "https":
                    options["server_hostname"] = parsed.hostname
                async with session.request("GET", target, **options) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        destination = urljoin(url, response.headers.get("Location", ""))
                        if url.startswith("https:") and not destination.startswith("https:"):
                            raise VisionError("image_source")
                        url = destination
                        continue
                    if response.status != 200:
                        raise VisionError("image_source")
                    if response.content_length and response.content_length > MAX_BYTES:
                        raise VisionError("image_limit")
                    data = bytearray()
                    async for part in response.content.iter_chunked(16384):
                        data.extend(part)
                        if len(data) > MAX_BYTES:
                            raise VisionError("image_limit")
                    return bytes(data)
        raise VisionError("image_source")

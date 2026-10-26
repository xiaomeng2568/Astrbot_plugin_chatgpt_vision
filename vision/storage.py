"""Atomic app-owned storage, DPAPI on Windows, owner-only files on POSIX."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import secrets
import stat
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from .errors import VisionError


def dpapi(data: bytes, decrypt: bool = False) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise VisionError("storage")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(result.pbData)


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory).absolute()
        self.path = self.directory / "oauth-state.bin"
        self.lock_path = self.directory / "oauth-state.lock"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise VisionError("storage")
        if os.name != "nt":
            mode = self.directory.stat()
            if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
                raise VisionError("storage")

    def load(self) -> dict:
        try:
            if not self.path.exists():
                return {"accounts": {}, "active": None}
            if self.path.is_symlink() or not self.path.is_file():
                raise ValueError()
            if os.name != "nt":
                mode = self.path.stat()
                if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
                    raise ValueError()
            raw = self.path.read_bytes()
            if raw.startswith(b"CV1D") and os.name == "nt":
                raw = dpapi(raw[4:], True)
            elif raw.startswith(b"CV1P") and os.name != "nt":
                raw = raw[4:]
            else:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("accounts"), dict):
                raise ValueError()
            return value
        except VisionError:
            raise
        except Exception:
            raise VisionError("storage") from None

    def save(self, value: dict) -> None:
        temporary = self.directory / ("state-" + secrets.token_hex(8) + ".tmp")
        try:
            data = json.dumps(value, ensure_ascii=False).encode()
            raw = b"CV1D" + dpapi(data) if os.name == "nt" else b"CV1P" + data
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except Exception:
            raise VisionError("storage") from None
        finally:
            temporary.unlink(missing_ok=True)

    @asynccontextmanager
    async def transaction(self, timeout: float = 8):
        deadline = time.monotonic() + timeout
        acquired = False
        try:
            while not acquired:
                try:
                    fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(fd)
                    acquired = True
                except FileExistsError:
                    if time.monotonic() >= deadline:
                        raise VisionError("busy")
                    await asyncio.sleep(0.05)
            state = self.load()
            yield state
        finally:
            if acquired:
                self.lock_path.unlink(missing_ok=True)

    async def host_id(self) -> str:
        async with self.transaction() as state:
            if not state.get("host_id"):
                state["host_id"] = "urn:uuid:" + str(uuid.uuid4())
                self.save(state)
            return state["host_id"]

    @staticmethod
    def active(state: dict) -> dict:
        account = state.get("accounts", {}).get(state.get("active"))
        if not isinstance(account, dict):
            raise VisionError("login_required")
        return account

    @staticmethod
    def clear_tokens(account: dict) -> None:
        for key in ("access_token", "refresh_token", "id_token", "selected_model", "verified_models"):
            account.pop(key, None)
        account["scopes"] = []

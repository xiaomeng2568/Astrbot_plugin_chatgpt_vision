"""OpenAI's documented public-client OAuth/OIDC flow; no shared client identity."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .errors import VisionError, http_error
from .storage import Store

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
JWKS = ISSUER + "/.well-known/jwks.json"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
TERMINAL_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired",
                    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"}


def b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def new_transaction(host_id: str, redirect_uri: str, account: dict | None = None) -> dict:
    parsed = urlsplit(redirect_uri)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or
            parsed.path != "/auth/callback" or not parsed.port or parsed.query or parsed.fragment):
        raise VisionError("auth_invalid")
    verifier = secrets.token_urlsafe(48)
    tx = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
          "verifier": verifier, "redirect_uri": redirect_uri, "expires": time.time() + 300,
          "client_id": account["client_id"] if account else "dynamic_agent_client",
          "subject": account.get("subject") if account else None, "used": False}
    parameters = {"client_id": tx["client_id"], "ext_agent_host_id": host_id,
                  "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPE,
                  "resource": RESOURCE, "state": tx["state"], "nonce": tx["nonce"],
                  "code_challenge_method": "S256",
                  "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
    if account is None:
        parameters["agent_name_hint"] = "AstrBot ChatGPT Vision"
    # Do not send retained id_token_hint; normal account selection remains available.
    tx["authorization_url"] = AUTHORIZE + "?" + urlencode(parameters)
    return tx


def consume_callback(tx: dict, query: dict) -> tuple[str, str]:
    if (tx["used"] or tx["expires"] <= time.time() or
            not hmac.compare_digest(str(query.get("state", "")), tx["state"])):
        raise VisionError("auth_invalid")
    tx["used"] = True
    if query.get("error") or not query.get("code"):
        raise VisionError("auth_invalid")
    client = query.get("client_id") or tx["client_id"]
    if (not isinstance(client, str) or not client.startswith("oaiapp_") or
            (tx["client_id"] != "dynamic_agent_client" and client != tx["client_id"])):
        raise VisionError("auth_invalid")
    return str(query["code"]), client


def validate_id_token(token: str, keys: dict, client: str, nonce: str | None,
                      subject: str | None = None) -> dict:
    try:
        head, body, signature = token.split(".")
        header, claims = json.loads(b64decode(head)), json.loads(b64decode(body))
        if header.get("alg") != "RS256" or header.get("crit"):
            raise ValueError()
        key = next(k for k in keys["keys"] if k.get("kid") == header.get("kid") and
                   k.get("kty") == "RSA" and k.get("alg", "RS256") == "RS256" and
                   k.get("use", "sig") == "sig")
        public = rsa.RSAPublicNumbers(int.from_bytes(b64decode(key["e"]), "big"),
                                     int.from_bytes(b64decode(key["n"]), "big")).public_key()
        public.verify(b64decode(signature), (head + "." + body).encode(), padding.PKCS1v15(), hashes.SHA256())
        audiences = claims.get("aud")
        audiences = audiences if isinstance(audiences, list) else [audiences]
        if (claims.get("iss") != ISSUER or client not in audiences or
                float(claims["exp"]) <= time.time() or
                float(claims.get("nbf", 0)) > time.time() + 30 or
                float(claims.get("iat", 0)) > time.time() + 30 or
                not isinstance(claims.get("sub"), str) or not claims["sub"]):
            raise ValueError()
        if len(audiences) > 1 and claims.get("azp") != client:
            raise ValueError()
        if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError()
        if subject is not None and claims["sub"] != subject:
            raise ValueError()
        return claims
    except Exception:
        raise VisionError("auth_invalid") from None


async def read_bounded(response, limit=1024 * 1024) -> bytes:
    data = bytearray()
    async for part in response.content.iter_chunked(16384):
        data.extend(part)
        if len(data) > limit:
            raise VisionError("protocol")
    return bytes(data)


class OAuth:
    def __init__(self, store: Store, session: aiohttp.ClientSession):
        self.store, self.session = store, session

    async def _json(self, url: str, form: dict | None = None) -> tuple[int, dict]:
        try:
            async with self.session.request("POST" if form is not None else "GET", url,
                                            data=form, allow_redirects=False,
                                            timeout=aiohttp.ClientTimeout(total=30)) as response:
                raw = await read_bounded(response)
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    value = {}
                return response.status, value if isinstance(value, dict) else {}
        except VisionError:
            raise
        except Exception:
            raise VisionError("network") from None

    async def finish(self, tx: dict, query: dict) -> str:
        code, client = consume_callback(tx, query)
        if tx["client_id"] == "dynamic_agent_client":
            # An issued client must survive a failed/expired code exchange, but is
            # not an authenticated account until OIDC identity validation succeeds.
            async with self.store.transaction() as state:
                state["pending_registration"] = client
                self.store.save(state)
        status, tokens = await self._json(TOKEN, {"grant_type": "authorization_code", "client_id": client,
                                                "code": code, "code_verifier": tx["verifier"],
                                                "redirect_uri": tx["redirect_uri"], "resource": RESOURCE})
        if status != 200:
            raise VisionError("auth_invalid")
        status, keys = await self._json(JWKS)
        if status != 200:
            raise VisionError("unavailable")
        claims = validate_id_token(tokens.get("id_token", ""), keys, client, tx["nonce"], tx["subject"])
        record = {"client_id": client, "issuer": ISSUER, "subject": claims["sub"]}
        self.apply_tokens(record, tokens, require_id=True)
        identity = hashlib.sha256((client + "\n" + claims["sub"]).encode()).hexdigest()[:24]
        async with self.store.transaction() as state:
            state["accounts"][identity] = record
            state["active"] = identity
            state.pop("pending_registration", None)
            self.store.save(state)
        return identity

    @staticmethod
    def apply_tokens(account: dict, tokens: dict, require_id=False) -> None:
        if (str(tokens.get("token_type", "")).lower() != "bearer" or
                not isinstance(tokens.get("access_token"), str) or not tokens["access_token"] or
                not isinstance(tokens.get("refresh_token"), str) or not tokens["refresh_token"] or
                not isinstance(tokens.get("scope"), str)):
            raise VisionError("auth_invalid")
        try:
            expires = float(tokens["expires_in"])
            if not 0 < expires <= 86400:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise VisionError("auth_invalid") from None
        account.update({"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
                        "scopes": tokens["scope"].split(), "expires_at": time.time() + expires})
        if tokens.get("id_token"):
            account["id_token"] = tokens["id_token"]
        elif require_id:
            raise VisionError("auth_invalid")

    async def access(self, rejected_token: str | None = None) -> tuple[str, str]:
        async with self.store.transaction() as state:
            account = self.store.active(state)
            if not account.get("access_token"):
                raise VisionError("login_required")
            if not {"resource.invoke", "chatgpt.tokens.use.direct"}.issubset(set(account.get("scopes", []))):
                raise VisionError("permission")
            expired = account.get("expires_at", 0) <= time.time() + 90
            rejected = rejected_token is not None and rejected_token == account["access_token"]
            if expired or rejected:
                status, tokens = await self._json(TOKEN, {"grant_type": "refresh_token",
                                                        "client_id": account["client_id"],
                                                        "refresh_token": account.get("refresh_token", ""),
                                                        "resource": RESOURCE})
                if status != 200:
                    refresh_error = tokens.get("error")
                    if isinstance(refresh_error, dict):
                        refresh_error = refresh_error.get("code", "")
                    if isinstance(refresh_error, str) and refresh_error in TERMINAL_REFRESH:
                        self.store.clear_tokens(account)
                        self.store.save(state)
                        raise VisionError("login_required")
                    raise http_error(status)
                if tokens.get("id_token"):
                    status_j, keys = await self._json(JWKS)
                    if status_j != 200:
                        raise VisionError("unavailable")
                    validate_id_token(tokens["id_token"], keys, account["client_id"], None, account["subject"])
                self.apply_tokens(account, tokens)
                self.store.save(state)
                if not {"resource.invoke", "chatgpt.tokens.use.direct"}.issubset(set(account["scopes"])):
                    raise VisionError("permission")
            return account["access_token"], state["active"]

    async def logout(self) -> bool:
        confirmed = False
        async with self.store.transaction() as state:
            account = self.store.active(state)
            if account.get("refresh_token"):
                try:
                    status, discovery = await self._json(DISCOVERY)
                    endpoint = discovery.get("revocation_endpoint", "")
                    parsed = urlsplit(endpoint)
                    if (status == 200 and parsed.scheme == "https" and parsed.hostname == "auth.openai.com"
                            and parsed.port in (None, 443) and not parsed.username and not parsed.password):
                        status, _ = await self._json(endpoint, {"token": account["refresh_token"],
                                                               "token_type_hint": "refresh_token",
                                                               "client_id": account["client_id"]})
                        confirmed = status == 200
                except VisionError:
                    pass
            self.store.clear_tokens(account)
            self.store.save(state)
        return confirmed

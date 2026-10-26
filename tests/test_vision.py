"""Offline tests: fake credentials, generated images, simulated platform events."""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import logging
import os
import sys
import tempfile
import time
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from PIL import Image, ImageDraw

from vision.client import (VisionClient, capture_usage, make_probe, plain_output, read_answer, request_body)
from vision.errors import VisionError
from vision.images import ImageReader, PublicResolver, check_url, normalize_image, read_local
from vision.oauth import ISSUER, JWKS, TOKEN, OAuth, consume_callback, new_transaction, validate_id_token
from vision.storage import Store
from vision.network import RoutedSession, choose_proxy

SENTINEL = "FAKE_TOKEN_SENTINEL_never_real_937461"
PRIVATE_IMAGE = "PRIVATE_IMAGE_SENTINEL_48291"


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class TokenFixture:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        self.jwks = {"keys": [{"kid": "test", "kty": "RSA", "alg": "RS256", "use": "sig",
                              "n": encode(numbers.n.to_bytes(256, "big")), "e": encode(numbers.e.to_bytes(3, "big"))}]}

    def token(self, **override):
        claims = {"iss": ISSUER, "aud": "oaiapp_test", "sub": "test-subject", "nonce": "test-nonce", "exp": time.time()+3600}
        claims.update(override)
        head = encode(json.dumps({"alg": "RS256", "kid": "test"}).encode())
        body = encode(json.dumps(claims).encode())
        signature = self.key.sign((head+"."+body).encode(), padding.PKCS1v15(), hashes.SHA256())
        return head+"."+body+"."+encode(signature)


def record(expired=False):
    return {"client_id": "oaiapp_test", "subject": "test-subject", "access_token": SENTINEL,
            "refresh_token": "FAKE_REFRESH_937461", "id_token": "FAKE_ID_937461",
            "scopes": ["resource.invoke", "chatgpt.tokens.use.direct"],
            "expires_at": time.time() + (-1 if expired else 3600),
            "selected_model": "account-model", "verified_models": {"account-model": time.time()}}


def token_response():
    return {"access_token": "NEW_FAKE_ACCESS_937461", "refresh_token": "NEW_FAKE_REFRESH_937461",
            "token_type": "Bearer", "scope": "resource.invoke chatgpt.tokens.use.direct",
            "expires_in": 3600}


def frame(data):
    return ("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode()


def completed(text="小猫在窗边发呆。"):
    return {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": text}]}]}}


class Content:
    def __init__(self, raw, split=13):
        self.raw, self.split = raw, split

    async def iter_chunked(self, _size):
        for i in range(0, len(self.raw), self.split):
            yield self.raw[i:i+self.split]


class Response:
    def __init__(self, status=200, data=None, raw=None, headers=None):
        self.status = status
        self.headers = headers or {"Content-Type": "text/event-stream"}
        self.content = Content(raw if raw is not None else json.dumps(data or {}).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"private")
        self.store.save({"accounts": {"account": record()}, "active": "account", "host_id": "urn:uuid:test"})

    def tearDown(self):
        self.tmp.cleanup()


class AuthTests(Base):
    @classmethod
    def setUpClass(cls):
        cls.fixture = TokenFixture()

    def test_valid_signature_identity(self):
        self.assertEqual(validate_id_token(self.fixture.token(), self.fixture.jwks,
                         "oaiapp_test", "test-nonce")["sub"], "test-subject")

    def test_invalid_claims_are_private(self):
        for override in ({"iss":"https://evil.invalid"}, {"aud":"other"}, {"exp":0},
                         {"nonce":"other"}, {"sub":""}, {"nbf":time.time()+1000}):
            with self.subTest(override=override), self.assertRaises(VisionError) as error:
                validate_id_token(self.fixture.token(**override), self.fixture.jwks, "oaiapp_test", "test-nonce")
            self.assertNotIn("evil", str(error.exception))

    def test_wrong_key_and_subject(self):
        with self.assertRaises(VisionError):
            validate_id_token(self.fixture.token(), TokenFixture().jwks, "oaiapp_test", "test-nonce")
        with self.assertRaises(VisionError):
            validate_id_token(self.fixture.token(), self.fixture.jwks, "oaiapp_test", "test-nonce", "other-account")

    def test_pkce_and_callback_one_time(self):
        from urllib.parse import parse_qs, urlsplit
        tx = new_transaction("urn:uuid:test", "http://127.0.0.1:14558/auth/callback")
        params = parse_qs(urlsplit(tx["authorization_url"]).query)
        self.assertEqual(params["client_id"], ["dynamic_agent_client"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertNotIn("client_secret", params)
        self.assertNotIn("id_token_hint", params)
        code, client = consume_callback(tx, {"code":"fake", "state":tx["state"], "client_id":"oaiapp_test"})
        self.assertEqual((code, client), ("fake", "oaiapp_test"))
        with self.assertRaises(VisionError):
            consume_callback(tx, {"code":"fake", "state":tx["state"], "client_id":"oaiapp_test"})

    def test_callback_state_and_new_client_required(self):
        tx = new_transaction("host", "http://127.0.0.1:14558/auth/callback")
        with self.assertRaises(VisionError):
            consume_callback(tx, {"state":"wrong", "code":"fake", "client_id":"oaiapp_test"})
        self.assertFalse(tx["used"])
        with self.assertRaises(VisionError):
            consume_callback(tx, {"state":tx["state"], "code":"fake"})

    def test_returning_client_cannot_be_replaced(self):
        tx = new_transaction("host", "http://127.0.0.1:14558/auth/callback", record())
        with self.assertRaises(VisionError):
            consume_callback(tx, {"state":tx["state"], "code":"fake", "client_id":"oaiapp_other"})

    def test_denied_and_expired_callback(self):
        for denied in (True, False):
            tx = new_transaction("host", "http://127.0.0.1:14558/auth/callback")
            if not denied:
                tx["expires"] = 0
            with self.assertRaises(VisionError):
                consume_callback(tx, {"state":tx["state"], "error":"access_denied"} if denied else
                                 {"state":tx["state"], "code":"fake", "client_id":"oaiapp_test"})

    def test_callback_only_exact_loopback(self):
        for uri in ("http://localhost:14558/auth/callback", "http://0.0.0.0:14558/auth/callback", "https://127.0.0.1:14558/auth/callback", "http://127.0.0.1:14558/callback"):
            with self.assertRaises(VisionError):
                new_transaction("host", uri)

    async def test_code_exchange_uses_issued_client(self):
        tx = new_transaction("host", "http://127.0.0.1:14558/auth/callback")
        tokens = token_response()
        tokens["id_token"] = self.fixture.token(nonce=tx["nonce"])
        session = Session([Response(data=tokens), Response(data=self.fixture.jwks)])
        await OAuth(self.store, session).finish(tx, {"state":tx["state"], "code":"test-code", "client_id":"oaiapp_test"})
        call = session.calls[0]
        self.assertEqual(call[1], TOKEN)
        self.assertEqual(call[2]["data"]["client_id"], "oaiapp_test")
        self.assertFalse(call[2]["allow_redirects"])
        self.assertTrue(self.store.active(self.store.load())["access_token"])

    async def test_missing_scope_does_not_call_network(self):
        state = self.store.load()
        state["accounts"]["account"]["scopes"] = ["openid"]
        self.store.save(state)
        session = Session([])
        with self.assertRaises(VisionError) as error:
            await OAuth(self.store, session).access()
        self.assertEqual(error.exception.code, "permission")
        self.assertEqual(session.calls, [])

    async def test_refresh_rotation_is_atomic(self):
        state = self.store.load();state["accounts"]["account"] = record(True);self.store.save(state)
        session = Session([Response(data=token_response())])
        oauth = OAuth(self.store, session)
        await asyncio.gather(oauth.access(), oauth.access())
        self.assertEqual(len(session.calls), 1)
        result = self.store.active(self.store.load())
        self.assertEqual(result["refresh_token"], "NEW_FAKE_REFRESH_937461")
        self.assertNotIn("scope", session.calls[0][2]["data"])

    async def test_terminal_refresh_clears_tokens_keeps_registration(self):
        state=self.store.load();state["accounts"]["account"]=record(True);self.store.save(state)
        with self.assertRaises(VisionError):
            await OAuth(self.store, Session([Response(400,{"error":"invalid_grant"})])).access()
        saved=self.store.active(self.store.load())
        self.assertNotIn("access_token",saved)
        self.assertEqual(saved["client_id"],"oaiapp_test")

    async def test_temporary_refresh_keeps_credentials(self):
        state=self.store.load();state["accounts"]["account"]=record(True);self.store.save(state)
        with self.assertRaises(VisionError):
            await OAuth(self.store,Session([Response(503,{"error":{"message":SENTINEL}})])).access()
        self.assertEqual(self.store.active(self.store.load())["access_token"],SENTINEL)

    async def test_failed_initial_exchange_keeps_only_pending_client(self):
        tx=new_transaction('host','http://127.0.0.1:14558/auth/callback')
        oauth=OAuth(self.store,Session([Response(400,{'error':'invalid_grant'})]))
        with self.assertRaises(VisionError):
            await oauth.finish(tx,{'state':tx['state'],'code':'expired-code','client_id':'oaiapp_retry'})
        state=self.store.load()
        self.assertEqual(state['pending_registration'],'oaiapp_retry')
        self.assertEqual(state['active'],'account')
        self.assertEqual(state['accounts']['account']['access_token'],SENTINEL)

    async def test_file_lock_conflict(self):
        async with self.store.transaction():
            with self.assertRaises(VisionError) as error:
                async with self.store.transaction(timeout=0.01):
                    pass
        self.assertEqual(error.exception.code,"busy")

    def test_storage_at_rest(self):
        raw=self.store.path.read_bytes()
        if os.name=="nt":
            self.assertNotIn(SENTINEL.encode(),raw)
            self.assertTrue(raw.startswith(b"CV1D"))
        else:
            self.assertEqual(self.store.path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.store.active(self.store.load())["access_token"],SENTINEL)

    async def test_logout_revokes_refresh_then_clears(self):
        session=Session([Response(data={"revocation_endpoint":ISSUER+"/api/accounts/oauth/revoke"}),Response(raw=b"")])
        self.assertTrue(await OAuth(self.store,session).logout())
        self.assertEqual(session.calls[-1][2]["data"]["token_type_hint"],"refresh_token")
        self.assertNotIn("access_token",self.store.active(self.store.load()))


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_bom_named_sse_and_all_line_endings(self):
        body=completed()
        body.pop('type')
        raw=('event: response.completed\ndata: '+json.dumps(body,ensure_ascii=False)+'\n\n').encode()
        for ending in (b'\n',b'\r\n',b'\r'):
            result=await read_answer(Content(b'\xef\xbb\xbf'+raw.replace(b'\n',ending),1))
            self.assertEqual(result,'小猫在窗边发呆。')

    async def test_terminal_success_does_not_wait_for_transport_eof(self):
        class LingeringContent:
            async def iter_chunked(self, size):
                yield frame(completed())
                raise asyncio.TimeoutError(SENTINEL + PRIVATE_IMAGE)
        diagnostic = {}
        result = await read_answer(LingeringContent(), diagnostic)
        self.assertEqual(result, '小猫在窗边发呆。')
        self.assertEqual(diagnostic['last_event'], 'response.completed')

    async def test_stream_diagnostic_omits_payload_and_unknown_event_names(self):
        raw = frame({'type': SENTINEL, 'delta': PRIVATE_IMAGE}) + frame({'type':'response.failed',
                    'response': {'error': {'code':SENTINEL,'message':PRIVATE_IMAGE,'param':PRIVATE_IMAGE}}})
        diagnostic = {}
        with self.assertRaises(VisionError):
            await read_answer(Content(raw), diagnostic)
        serialized = json.dumps(diagnostic)
        self.assertNotIn(SENTINEL, serialized)
        self.assertNotIn(PRIVATE_IMAGE, serialized)
        self.assertEqual(diagnostic['event_counts']['other'], 1)
        self.assertEqual(diagnostic['api_code'], 'unrecognized')

    async def test_fragmented_chinese_crlf_stream(self):
        raw=frame({"type":"response.output_text.delta","delta":"小猫"})+frame({"type":"response.output_text.delta","delta":"在发呆。"})+frame(completed("小猫在发呆。"))
        self.assertEqual(await read_answer(Content(raw.replace(b"\n",b"\r\n"),1)),"小猫在发呆。")

    async def test_completed_without_deltas(self):
        self.assertEqual(await read_answer(Content(frame(completed()))),"小猫在窗边发呆。")

    async def test_partial_then_quota_never_returns_partial(self):
        raw=frame({"type":"response.output_text.delta","delta":PRIVATE_IMAGE})+frame({"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded","message":SENTINEL}}})
        with self.assertRaises(VisionError) as error:
            await read_answer(Content(raw))
        self.assertEqual(error.exception.code,"quota")
        self.assertNotIn(PRIVATE_IMAGE,str(error.exception))
        self.assertNotIn(SENTINEL,str(error.exception))

    async def test_incomplete_error_and_disconnect(self):
        for raw in [frame({"type":"response.incomplete"}),frame({"type":"error","error":{"message":SENTINEL}}),frame({"type":"response.output_text.delta","delta":PRIVATE_IMAGE}),b'data: {"type":"response.completed"']:
            with self.subTest(raw=raw),self.assertRaises(VisionError) as error:
                await read_answer(Content(raw))
            self.assertNotIn(SENTINEL,str(error.exception))
            self.assertNotIn(PRIVATE_IMAGE,str(error.exception))

    async def test_refusal_not_success(self):
        with self.assertRaises(VisionError) as error:
            await read_answer(Content(frame({"type":"response.refusal.delta","delta":"no"})+frame(completed())))
        self.assertEqual(error.exception.code,"refused")

    async def test_done_without_completed_is_failure(self):
        with self.assertRaises(VisionError):
            await read_answer(Content(b'data: [DONE]\n\n'))

    async def test_bad_json_and_oversized_frame(self):
        for raw in [b'data: invalid\n\n',b'x'*1024*1024+b'x']:
            with self.assertRaises(VisionError):
                await read_answer(Content(raw,8192))

    def test_request_contains_only_supported_fields(self):
        body=request_body("account-model","描述",["data:image/png;base64,AA=="],"天然呆")
        self.assertFalse(body["store"]);self.assertTrue(body["stream"])
        self.assertIsInstance(body["input"],list)
        self.assertEqual(body["input"][0]["content"][1]["type"],"input_image")
        self.assertFalse(set(body)&{"tools","max_output_tokens","temperature","previous_response_id","user","metadata"})

    def test_plain_reply_and_secret_scrubbing(self):
        text=plain_output("## 小猫\n**发呆** `喵`\n"+SENTINEL+" data:image/png;base64,"+PRIVATE_IMAGE,500,[SENTINEL])
        for value in ["##","**","`",SENTINEL,PRIVATE_IMAGE]:self.assertNotIn(value,text)
        self.assertIn("小猫",text)
        self.assertIn("截断",plain_output("字"*600,100))


class ImageTests(Base):
    def picture(self,metadata=False):
        image=Image.new("RGB",(80,40),"white")
        ImageDraw.Draw(image).text((5,5),"TEST",fill="black")
        output=BytesIO()
        if metadata:
            from PIL.PngImagePlugin import PngInfo
            info=PngInfo();info.add_text("private",PRIVATE_IMAGE)
            image.save(output,format="PNG",pnginfo=info)
        else:image.save(output,format="PNG")
        return output.getvalue()

    def test_metadata_removed(self):
        result=normalize_image(self.picture(True))
        raw=base64.b64decode(result.split(",")[1])
        self.assertNotIn(PRIVATE_IMAGE.encode(),raw)
        with Image.open(BytesIO(raw)) as image:self.assertEqual(image.info,{})

    def test_fake_or_oversized_images_rejected(self):
        for data in [b'not-image '+PRIVATE_IMAGE.encode(),b'x'*(8*1024*1024+1)]:
            with self.assertRaises(VisionError) as error:normalize_image(data)
            self.assertNotIn(PRIVATE_IMAGE,str(error.exception))

    def test_animated_and_pixel_bomb_rejected(self):
        first=Image.new("RGB",(10,10),"red");second=Image.new("RGB",(10,10),"blue")
        buf=BytesIO();first.save(buf,format="GIF",save_all=True,append_images=[second])
        with self.assertRaises(VisionError):normalize_image(buf.getvalue())
        with patch("vision.images.MAX_PIXELS",100):
            with self.assertRaises(VisionError):normalize_image(self.picture())

    def test_local_boundaries_and_unc(self):
        allowed=Path(self.tmp.name)/"pictures";allowed.mkdir()
        inside=allowed/"inside.png";inside.write_bytes(self.picture())
        outside=Path(self.tmp.name)/"outside.png";outside.write_bytes(self.picture())
        self.assertEqual(read_local(str(inside),[allowed]),self.picture())
        for path in [str(outside),'file://evil-host/share/image.png','\\\\server\\share\\x.png','relative.png']:
            with self.assertRaises(VisionError):read_local(path,[allowed])

    def test_private_urls_and_credentials_rejected(self):
        for url in ['http://127.0.0.1/x','http://169.254.169.254/latest','http://[::1]/x','http://10.0.0.1/x','http://localhost/x','http://name.local/x','https://user:password@example.com/x','https://example.com:8080/x','file:///secret']:
            with self.assertRaises(VisionError):check_url(url)
        check_url('https://example.com/public-image?signature=allowed-only-in-memory')

    async def test_dns_rebinding_answer_rejected(self):
        loop=asyncio.get_running_loop()
        with patch.object(loop,"getaddrinfo",new=AsyncMock(return_value=[(2,1,6,'',('127.0.0.1',443))])):
            with self.assertRaises(VisionError):await PublicResolver().resolve('public-looking.invalid',443)

    async def test_data_uri_is_reencoded(self):
        reader=ImageReader([])
        data=self.picture(True)
        result,size=await reader.read('base64://'+base64.b64encode(data).decode())
        self.assertEqual(size,len(data));self.assertTrue(result.startswith('data:image/png;base64,'))
        self.assertNotIn(PRIVATE_IMAGE,base64.b64decode(result.split(',')[1]).decode('latin1'))


class ClientTests(Base):
    async def test_full_self_test_with_actual_client_steps(self):
        _,image=make_probe()
        session=Session([self.catalog(),Response(raw=frame(completed('OK'))),self.catalog(),
                         Response(raw=frame(completed('ABC234')),headers={'Content-Type':'application/octet-stream'}),self.catalog()])
        client=VisionClient(OAuth(self.store,session));phases=[]
        with patch('vision.client.make_probe',return_value=('ABC234',image)):
            self.assertTrue(await client.self_test('account-model',phases.append))
        self.assertEqual(phases,['text','vision','select'])
        self.assertEqual(len(session.calls),5)
        self.assertEqual(self.store.active(self.store.load())['selected_model'],'account-model')

    async def test_failed_reverification_revokes_old_model_confirmation(self):
        client=VisionClient(OAuth(self.store,Session([self.catalog(),Response(raw=frame(completed('wrong')))])))
        with self.assertRaises(VisionError):await client.verify_model('account-model')
        account=self.store.active(self.store.load())
        self.assertNotIn('selected_model',account)
        self.assertNotIn('account-model',account.get('verified_models',{}))

    async def test_octet_stream_header_with_valid_sse_is_accepted(self):
        response=Response(raw=frame(completed()),headers={'Content-Type':'application/octet-stream'})
        client=VisionClient(OAuth(self.store,Session([response])))
        result,_=await client._call('https://api.openai.com/v1/responses',{'test':True})
        self.assertEqual(result,'小猫在窗边发呆。')
        self.assertTrue(client.diagnostic['header_body_mismatch'])
        self.assertEqual(client.diagnostic['body_format'],'sse')

    async def test_missing_content_type_with_valid_sse_is_accepted(self):
        response=Response(raw=frame(completed()),headers={'X-Test':'test'})
        client=VisionClient(OAuth(self.store,Session([response])))
        result,_=await client._call('https://api.openai.com/v1/responses',{'test':True})
        self.assertEqual(result,'小猫在窗边发呆。')
        self.assertEqual(client.diagnostic['content_type'],'missing')

    async def test_200_json_error_is_not_stream_success(self):
        response=Response(data={'error':{'code':'subscription_sharing_usage_limit_exceeded','message':SENTINEL+PRIVATE_IMAGE}},
                          headers={'Content-Type':'application/octet-stream'})
        client=VisionClient(OAuth(self.store,Session([response])))
        with self.assertRaises(VisionError) as error:
            await client._call('https://api.openai.com/v1/responses',{'test':True})
        self.assertEqual(error.exception.code,'quota')
        self.assertEqual(client.diagnostic['failure_reason'],'json_error_response')
        self.assertNotIn(SENTINEL,json.dumps(client.diagnostic))

    async def test_non_stream_completed_json_cannot_confirm_vision(self):
        response=Response(data=completed()['response'],headers={'Content-Type':'application/json'})
        client=VisionClient(OAuth(self.store,Session([response])))
        with self.assertRaises(VisionError):
            await client._call('https://api.openai.com/v1/responses',{'test':True})
        self.assertEqual(client.diagnostic['failure_reason'],'non_stream_response')

    async def test_self_test_selects_only_after_both_successes(self):
        client=VisionClient(OAuth(self.store,Session([])))
        client.check_text=AsyncMock(return_value=True)
        client.verify_model=AsyncMock(return_value=True)
        client.select=AsyncMock()
        phases=[]
        self.assertTrue(await client.self_test('account-model',phases.append))
        self.assertEqual(phases,['text','vision','select'])
        client.select.assert_awaited_once_with('account-model')

    async def test_self_test_stops_without_image_on_text_failure(self):
        client=VisionClient(OAuth(self.store,Session([])))
        client.check_text=AsyncMock(side_effect=VisionError('quota'))
        client.verify_model=AsyncMock();client.select=AsyncMock()
        with self.assertRaises(VisionError):await client.self_test('account-model')
        client.verify_model.assert_not_awaited();client.select.assert_not_awaited()

    async def test_real_http_octet_sse_and_lingering_transport(self):
        from aiohttp import web
        release=asyncio.Event()
        async def handler(request):
            await request.read()
            response=web.StreamResponse(headers={'Content-Type':'application/octet-stream'})
            await response.prepare(request)
            await response.write(frame(completed('OK')))
            await release.wait()
            return response
        app=web.Application();app.router.add_post('/responses',handler)
        runner=web.AppRunner(app,access_log=None);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        try:
            with patch('vision.network.choose_proxy',return_value=None):
                async with RoutedSession() as session:
                    client=VisionClient(OAuth(self.store,session))
                    result,_=await asyncio.wait_for(client._call(f'http://127.0.0.1:{port}/responses',{'test':True}),2)
            self.assertEqual(result,'OK')
            self.assertEqual(client.diagnostic['body_format'],'sse')
        finally:
            release.set();await runner.cleanup()

    async def test_http_error_diagnostic_is_allowlisted(self):
        response = Response(400, {'error': {'code':'unsupported_parameter','type':'invalid_request_error',
                            'param':'input[0].content[1].image_url','message':SENTINEL+PRIVATE_IMAGE}},
                            headers={'Content-Type':'application/json','x-request-id':'req_test_123'})
        session = Session([response]); client = VisionClient(OAuth(self.store, session))
        with self.assertRaises(VisionError):
            await client._call('https://api.openai.com/v1/responses', {'test':True})
        diagnostic = client.diagnostic
        self.assertEqual(diagnostic['http_status'],400)
        self.assertEqual(diagnostic['api_code'],'unsupported_parameter')
        self.assertEqual(diagnostic['api_param'],'input[0].content[1].image_url')
        self.assertEqual(diagnostic['request_id'],'req_test_123')
        self.assertNotIn(SENTINEL,json.dumps(diagnostic))
        self.assertNotIn(PRIVATE_IMAGE,json.dumps(diagnostic))

    async def test_html_response_reports_format_without_content(self):
        response=Response(raw=('<html>'+SENTINEL+PRIVATE_IMAGE+'</html>').encode(),headers={'Content-Type':'text/html'})
        client=VisionClient(OAuth(self.store,Session([response])))
        with self.assertRaises(VisionError):
            await client._call('https://api.openai.com/v1/responses', {'test':True})
        self.assertEqual(client.diagnostic['content_type'],'text/html')
        self.assertEqual(client.diagnostic['failure_reason'],'html_response')
        self.assertNotIn(SENTINEL,json.dumps(client.diagnostic))

    async def test_timeout_reports_stage_without_exception_text(self):
        class TimeoutSession:
            def request(self,*args,**kwargs):
                raise asyncio.TimeoutError(SENTINEL+PRIVATE_IMAGE)
        client=VisionClient(OAuth(self.store,TimeoutSession()),180)
        with self.assertRaises(VisionError) as error:
            await client._call('https://api.openai.com/v1/responses',{'test':True})
        self.assertEqual(error.exception.code,'timeout')
        self.assertEqual(client.diagnostic['stage'],'http_connect')
        self.assertEqual(client.diagnostic['timeout_seconds'],180)
        self.assertNotIn(SENTINEL,json.dumps(client.diagnostic))

    async def test_text_only_probe_cannot_set_visual_verification(self):
        state=self.store.load();state['accounts']['account'].pop('verified_models');self.store.save(state)
        session=Session([self.catalog(),Response(raw=frame(completed('OK')))])
        client=VisionClient(OAuth(self.store,session))
        self.assertTrue(await client.check_text('account-model'))
        body=session.calls[-1][2]['json']
        self.assertFalse(body['store']);self.assertTrue(body['stream'])
        self.assertEqual([c['type'] for c in body['input'][0]['content']],['input_text'])
        self.assertNotIn('verified_models',self.store.active(self.store.load()))

    def catalog(self):
        return Response(data={"models":[{"slug":"account-model","display_name":"Account model","visibility":"list"},
                                        {"slug":"hidden","visibility":"hide"}]})

    async def test_catalog_uses_account_not_hardcoded(self):
        session=Session([self.catalog()]);client=VisionClient(OAuth(self.store,session))
        rows,identity=await client.models()
        self.assertEqual([r['slug'] for r in rows],['account-model']);self.assertEqual(identity,'account')
        self.assertEqual(session.calls[0][1],'https://api.openai.com/v1/models')

    async def test_synthetic_vision_verification_and_selection(self):
        _,image=make_probe()
        session=Session([self.catalog(),Response(raw=frame(completed('ABC234'))),self.catalog()])
        client=VisionClient(OAuth(self.store,session))
        with patch('vision.client.make_probe',return_value=('ABC234',image)):
            self.assertTrue(await client.verify_model('account-model'))
        await client.select('account-model')
        self.assertEqual(self.store.active(self.store.load())['selected_model'],'account-model')

    async def test_incorrect_probe_does_not_confirm_vision(self):
        state=self.store.load();state['accounts']['account'].pop('verified_models');self.store.save(state)
        client=VisionClient(OAuth(self.store,Session([self.catalog(),Response(raw=frame(completed('wrong')))])))
        with self.assertRaises(VisionError):await client.verify_model('account-model')
        self.assertNotIn('verified_models',self.store.active(self.store.load()))

    async def test_model_not_in_account_cannot_be_selected(self):
        client=VisionClient(OAuth(self.store,Session([self.catalog()])))
        with self.assertRaises(VisionError):await client.select('imaginary-model')

    async def test_expired_verification_blocks_private_image(self):
        state=self.store.load();state['accounts']['account']['verified_models']['account-model']=0;self.store.save(state)
        session=Session([]);client=VisionClient(OAuth(self.store,session))
        with self.assertRaises(VisionError):await client.describe('OCR',[PRIVATE_IMAGE])
        self.assertEqual(session.calls,[])

    async def test_describe_complete_reply_and_no_credentials_in_payload(self):
        session=Session([self.catalog(),Response(raw=frame(completed('## 小猫\n**发呆** '+SENTINEL)))])
        client=VisionClient(OAuth(self.store,session))
        _,image=make_probe();text=await client.describe('描述',[image],'天然呆')
        self.assertNotIn(SENTINEL,text);self.assertNotIn('**',text)
        body=session.calls[1][2]['json']
        self.assertNotIn(SENTINEL,json.dumps(body));self.assertFalse(session.calls[1][2]['allow_redirects'])

    async def test_401_refreshes_once_before_stream(self):
        session=Session([Response(401,{'detail':SENTINEL}),Response(data=token_response()),Response(raw=frame(completed()))])
        client=VisionClient(OAuth(self.store,session))
        result,_=await client._call('https://api.openai.com/v1/responses',request_body('account-model','描述',['data:image/png;base64,AA==']))
        self.assertEqual(result,'小猫在窗边发呆。');self.assertEqual(len(session.calls),3)
        self.assertEqual(session.calls[1][1],TOKEN)

    async def test_quota_no_retry_no_fallback_and_no_private_error(self):
        session=Session([Response(429,{'detail':SENTINEL+' '+PRIVATE_IMAGE},headers={'Retry-After':'12'})])
        client=VisionClient(OAuth(self.store,session))
        with self.assertRaises(VisionError) as error:await client._call('https://api.openai.com/v1/responses',{'test':True})
        self.assertEqual(error.exception.code,'quota');self.assertNotIn(SENTINEL,str(error.exception))
        with self.assertRaises(VisionError):await client.models()
        self.assertEqual(len(session.calls),1)

    async def test_logout_during_request_suppresses_result(self):
        client=VisionClient(OAuth(self.store,Session([self.catalog()])))
        async def fake_call(*args,**kwargs):
            state=self.store.load();self.store.clear_tokens(state['accounts']['account']);self.store.save(state)
            return PRIVATE_IMAGE,'account'
        original=client.models
        client.models=original
        with patch.object(client,'_call',side_effect=fake_call):
            client.models=AsyncMock(return_value=([{'slug':'account-model'}],'account'))
            with self.assertRaises(VisionError) as error:
                await client.describe('描述',['data:image/png;base64,AA=='])
        self.assertNotIn(PRIVATE_IMAGE,str(error.exception))


def load_plugin():
    def decorate(*args,**kwargs):
        return lambda fn:fn
    class Star:
        def __init__(self,context):pass
    class ComponentImage:
        def __init__(self,url):self.url=url;self.file=''
    class Reply:
        def __init__(self,chain=None,id='42'):self.chain=chain;self.id=id
    api=types.ModuleType('astrbot.api');api.AstrBotConfig=dict;api.logger=logging.getLogger('vision-test')
    event=types.ModuleType('astrbot.api.event');event.AstrMessageEvent=object
    event.filter=types.SimpleNamespace(command=decorate,on_waiting_llm_request=decorate)
    star=types.ModuleType('astrbot.api.star');star.Context=object;star.Star=Star
    components=types.ModuleType('astrbot.api.message_components');components.Image=ComponentImage;components.Reply=Reply
    startools=types.ModuleType('astrbot.core.star.star_tools');startools.StarTools=types.SimpleNamespace(get_data_dir=lambda name:Path(tempfile.gettempdir())/name)
    paths=types.ModuleType('astrbot.core.utils.astrbot_path');paths.get_astrbot_temp_path=tempfile.gettempdir
    modules={'astrbot':types.ModuleType('astrbot'),'astrbot.api':api,'astrbot.api.event':event,'astrbot.api.star':star,
             'astrbot.api.message_components':components,'astrbot.core':types.ModuleType('astrbot.core'),
             'astrbot.core.star':types.ModuleType('astrbot.core.star'),'astrbot.core.star.star_tools':startools,
             'astrbot.core.utils':types.ModuleType('astrbot.core.utils'),'astrbot.core.utils.astrbot_path':paths}
    sys.modules.update(modules)
    package=Path(__file__).resolve().parents[1]
    namespace=types.ModuleType('vision_plugin');namespace.__path__=[str(package)];sys.modules['vision_plugin']=namespace
    # Reuse the exact tested modules rather than reimporting separate error classes.
    sys.modules['vision_plugin.vision']=sys.modules['vision']
    for name in ['client','errors','images','oauth','storage']:
        sys.modules['vision_plugin.vision.'+name]=sys.modules['vision.'+name]
    return importlib.import_module('vision_plugin.main'),ComponentImage,Reply


class Event:
    def __init__(self,images,group='',sender='123',admin=True):
        self.images=images;self.group=group;self.sender=sender;self.admin=admin
        self.message_obj=types.SimpleNamespace(message_id='qq-test-1',self_id='456')
        self.unified_msg_origin='qq:private:123';self.extra={};self.sent=[];self.stopped=False
    def get_messages(self):return self.images
    def get_extra(self,key,default=None):return self.extra.get(key,default)
    def set_extra(self,key,value):self.extra[key]=value
    def stop_event(self):self.stopped=True
    def get_group_id(self):return self.group
    def is_admin(self):return self.admin
    def get_sender_id(self):return self.sender
    def get_platform_name(self):return 'aiocqhttp'
    def get_message_str(self):return '请描述图片'
    def plain_result(self,text):return text
    async def send(self,text):self.sent.append(text)


class MessageTests(Base):
    @classmethod
    def setUpClass(cls):cls.plugin,cls.Image,cls.Reply=load_plugin()

    def plugin_instance(self,config):
        plugin=object.__new__(self.plugin.ChatGPTVisionPlugin)
        plugin.config=config;plugin.store=self.store;plugin.seen=__import__('collections').OrderedDict()
        plugin.busy=asyncio.Lock();plugin.context=object()
        plugin.reader=types.SimpleNamespace(read=AsyncMock(return_value=('data:image/png;base64,AA==',10)))
        plugin.fake_client=types.SimpleNamespace(describe=AsyncMock(return_value='小猫在发呆。'))
        plugin.get_client=lambda:plugin.fake_client
        return plugin

    def test_constructor_uses_plugin_owned_data_directory_without_changing_parent(self):
        parent = Path(self.tmp.name) / 'plugin_data'
        parent.mkdir(mode=0o755)
        before = parent.stat().st_mode
        def data_directory(name):
            self.assertEqual(name, 'astrbot_plugin_chatgpt_vision')
            (parent / name).mkdir(mode=0o755)
            return parent / name
        with patch.object(self.plugin.StarTools, 'get_data_dir', side_effect=data_directory) as resolver:
            plugin = self.plugin.ChatGPTVisionPlugin(object(), {})
        resolver.assert_called_once_with('astrbot_plugin_chatgpt_vision')
        self.assertEqual(plugin.store.path, parent / 'astrbot_plugin_chatgpt_vision' / 'oauth-state.bin')
        self.assertEqual(parent.stat().st_mode, before)
        if os.name != 'nt':
            self.assertEqual(plugin.data_dir.stat().st_mode & 0o777, 0o700)
        self.assertFalse((parent / 'oauth-state.bin').exists())
        self.assertEqual(plugin.store.load(), {'accounts': {}, 'active': None})

    def test_management_default_matches_runtime_namespace_even_if_checkout_is_renamed(self):
        from manage import default_data_dir
        data = Path(self.tmp.name) / 'data'
        plugin = data / 'plugins' / 'renamed-checkout'
        self.assertEqual(default_data_dir(plugin), (data / 'plugin_data' / 'astrbot_plugin_chatgpt_vision').resolve())

    def test_direct_and_optional_reply_extraction(self):
        event=Event([self.Image('first'),self.Reply([self.Image('quoted')]),self.Image('first')])
        self.assertEqual(self.plugin.image_references(event),['first'])
        self.assertEqual(self.plugin.image_references(event,True),['first','quoted'])

    def test_selected_group_defaults_to_all_members_with_legacy_ids(self):
        plugin=self.plugin_instance({'allowed_group_ids':['777']})
        self.assertTrue(plugin._authorized(Event([],group='777',sender='999',admin=False)))
        self.assertFalse(plugin._authorized(Event([],group='888',admin=True)))
        self.assertFalse(self.plugin_instance({})._authorized(Event([],group='777',admin=True)))

    def test_selected_group_umo_is_scoped_to_the_platform_instance(self):
        plugin=self.plugin_instance({'allowed_group_ids':['qq-a:GroupMessage:777']})
        event=Event([],group='777',admin=False)
        event.unified_msg_origin='qq-a:GroupMessage:777'
        self.assertTrue(plugin._authorized(event))
        event.unified_msg_origin='qq-b:GroupMessage:777'
        self.assertFalse(plugin._authorized(event))
        event.unified_msg_origin='qq-a:GroupMessage:888';event.group='888'
        self.assertFalse(plugin._authorized(event))

    def test_group_restriction_and_private_permissions_are_independent(self):
        config={'allowed_group_ids':['777'],'group_member_auth':True,
                'group_allowed_user_ids':['member'],'allowed_user_ids':['private']}
        plugin=self.plugin_instance(config)
        cases=[('777','member',False,True),('777','private',False,False),
               ('777','stranger',False,False),('777','admin',True,True),
               ('888','member',False,False),('888','admin',True,False),
               ('','member',False,False),('','private',False,True),
               ('','stranger',False,False),('','admin',True,True)]
        for group,sender,admin,expected in cases:
            with self.subTest(group=group,sender=sender,admin=admin):
                self.assertEqual(plugin._authorized(Event([],group,sender,admin)),expected)

    def test_disabling_group_restriction_ignores_retained_member_list(self):
        plugin=self.plugin_instance({'allowed_group_ids':['777'],'group_member_auth':False,
                                     'group_allowed_user_ids':['member']})
        self.assertTrue(plugin._authorized(Event([],group='777',sender='stranger',admin=False)))

    def test_empty_member_list_restricts_group_to_bot_admins_when_enabled(self):
        plugin=self.plugin_instance({'allowed_group_ids':['777'],'group_member_auth':True})
        self.assertFalse(plugin._authorized(Event([],group='777',sender='stranger',admin=False)))
        self.assertTrue(plugin._authorized(Event([],group='777',admin=True)))

    def test_private_defaults_preserve_old_user_list(self):
        plugin=self.plugin_instance({'allowed_user_ids':['123']})
        self.assertTrue(plugin._authorized(Event([],sender='123',admin=False)))
        self.assertFalse(plugin._authorized(Event([],sender='456',admin=False)))
        self.assertTrue(plugin._authorized(Event([],sender='456',admin=True)))

    async def test_group_without_member_restriction_can_reach_visual_model(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth','auto_reply':True,'allowed_group_ids':['777']})
        event=Event([self.Image('image')],group='777',sender='stranger',admin=False)
        await plugin.automatic_vision(event)
        self.assertEqual(event.sent,['小猫在发呆。'])
        plugin.fake_client.describe.assert_awaited_once()

    async def test_restricted_group_rejects_before_quote_fetch_and_image_read(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth','allowed_group_ids':['777'],
                                     'group_member_auth':True,'allowed_user_ids':['123']})
        event=self.quote_event(group='777',sender='123',admin=False)
        await plugin.describe_command(event)
        event.bot.call_action.assert_not_awaited()
        plugin.reader.read.assert_not_awaited();plugin.fake_client.describe.assert_not_awaited()

    def test_schema_uses_native_selectors_and_conditional_member_list(self):
        schema=json.loads((Path(__file__).resolve().parents[1]/'_conf_schema.json').read_text(encoding='utf-8'))
        self.assertEqual(schema['allowed_group_ids']['_special'],'umo')
        self.assertIs(schema['group_member_auth']['default'],False)
        self.assertEqual(schema['group_member_auth']['type'],'bool')
        self.assertEqual(schema['group_allowed_user_ids']['condition'],{'group_member_auth':True})
        self.assertEqual(schema['group_allowed_user_ids']['_special'],'sender_id')
        self.assertEqual(schema['allowed_user_ids']['_special'],'sender_id')
        self.assertNotIn('condition',schema['allowed_user_ids'])

    async def test_oauth_usage_bridge_reports_success_without_content(self):
        plugin=self.plugin_instance({})
        service=types.SimpleNamespace(ingest_external=AsyncMock())
        plugin.context=types.SimpleNamespace(get_registered_star=lambda name:types.SimpleNamespace(activated=True,star_cls=service))
        async def describe(*args):
            plugin.fake_client.last_meter={'started':True,'reported':False,'request_id':'request-one','model':'account-model',
                                           'usage':{'input_tokens':100,'output_tokens':20}}
            return 'private image description'
        plugin.fake_client.describe.side_effect=describe
        result=await plugin._metered_describe(Event([]),plugin.fake_client,'PRIVATE_PROMPT',[PRIVATE_IMAGE],'',500)
        self.assertEqual(result,'private image description')
        call=service.ingest_external.await_args.kwargs
        self.assertEqual(call['status'],'completed');self.assertEqual(call['usage']['input_tokens'],100)
        self.assertNotIn(PRIVATE_IMAGE,str(call));self.assertNotIn('PRIVATE_PROMPT',str(call))

    async def test_usage_bridge_failure_does_not_break_image_answer_or_leak_error(self):
        plugin=self.plugin_instance({})
        service=types.SimpleNamespace(ingest_external=AsyncMock(side_effect=RuntimeError(SENTINEL+' '+PRIVATE_IMAGE)))
        plugin.context=types.SimpleNamespace(get_registered_star=lambda name:types.SimpleNamespace(activated=True,star_cls=service))
        async def describe(*args):
            plugin.fake_client.last_meter={'started':True,'request_id':'one','usage':{},'model':'model'}
            return 'answer'
        plugin.fake_client.describe.side_effect=describe
        with self.assertLogs('vision-test',level='WARNING') as logs:
            result=await plugin._metered_describe(Event([]),plugin.fake_client,'',[], '',500)
        self.assertEqual(result,'answer');self.assertNotIn(SENTINEL,str(logs.output));self.assertNotIn(PRIVATE_IMAGE,str(logs.output))

    async def test_failed_image_request_is_reported_as_unknown_usage_and_original_failure_preserved(self):
        plugin=self.plugin_instance({})
        service=types.SimpleNamespace(ingest_external=AsyncMock())
        plugin.context=types.SimpleNamespace(get_registered_star=lambda name:types.SimpleNamespace(activated=True,star_cls=service))
        async def fail(*args):
            plugin.fake_client.last_meter={'started':True,'request_id':'one','usage':{},'model':'model'}
            raise VisionError('network')
        plugin.fake_client.describe.side_effect=fail
        with self.assertRaises(VisionError):await plugin._metered_describe(Event([]),plugin.fake_client,'',[], '',500)
        self.assertEqual(service.ingest_external.await_args.kwargs['status'],'failed')
        self.assertEqual(service.ingest_external.await_args.kwargs['usage'],{})

    def quote_event(self, result=None, **kwargs):
        event=Event([self.Reply()], **kwargs)
        if result is None:
            result={'message_id':42,'message_type':'private','sender':{'user_id':123},
                    'message':[{'type':'image','data':{'url':'https://cdn.example.com/fish.png'}}]}
        event.bot=types.SimpleNamespace(call_action=AsyncMock(return_value=result))
        return event

    async def test_quote_id_fetches_original_image_and_responds_once(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth'})
        event=self.quote_event()
        await plugin.describe_command(event)
        await plugin.describe_command(event)
        event.bot.call_action.assert_awaited_once_with(action='get_msg',message_id=42,self_id='456')
        plugin.reader.read.assert_awaited_once_with('https://cdn.example.com/fish.png')
        self.assertEqual(event.sent,['小猫在发呆。'])
        self.assertTrue(event.stopped)

    async def test_existing_quote_chain_does_not_fetch_again(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth'})
        event=self.quote_event();event.images=[self.Reply([self.Image('quoted')])]
        await plugin.describe_command(event)
        event.bot.call_action.assert_not_awaited()
        plugin.reader.read.assert_awaited_once_with('quoted')

    async def test_old_disabled_setting_is_respected_and_explained(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth','include_replied_images':False})
        event=self.quote_event();await plugin.describe_command(event)
        event.bot.call_action.assert_not_awaited();plugin.reader.read.assert_not_awaited()
        self.assertIn('include_replied_images',event.sent[0])

    async def test_raw_platform_reply_when_component_was_not_built(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth'})
        event=self.quote_event();event.images=[]
        event.message_obj.raw_message={'message':[{'type':'reply','data':{'id':'42'}}]}
        await plugin.describe_command(event)
        event.bot.call_action.assert_awaited_once()
        self.assertEqual(event.sent,['小猫在发呆。'])

    async def test_no_reply_fetch_for_unauthorized_user_or_group(self):
        for kwargs in [{'sender':'999','admin':False},{'group':'999'}]:
            plugin=self.plugin_instance({'backend':'chatgpt_oauth'})
            event=self.quote_event(**kwargs);await plugin.describe_command(event)
            event.bot.call_action.assert_not_awaited();plugin.reader.read.assert_not_awaited()

    async def test_auto_mode_does_not_read_quoted_images(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth','auto_reply':True})
        event=self.quote_event();await plugin.automatic_vision(event)
        event.bot.call_action.assert_not_awaited();self.assertFalse(event.stopped)

    async def test_reject_cross_conversation_and_wrong_message(self):
        results=[{'message_id':43}, {'message_id':42,'group_id':'999'},
                 {'message_id':42,'message_type':'group'},
                 {'message_id':42,'sender':{'user_id':'999'}},
                 {'message_id':42,'self_id':'999'},
                 {'message_id':42,'sender':{'user_id':'456'},'target_id':'999'}]
        for result in results:
            plugin=self.plugin_instance({'backend':'chatgpt_oauth'})
            event=self.quote_event(result)
            with self.assertLogs('vision-test',level='WARNING'):
                await plugin.describe_command(event)
            plugin.reader.read.assert_not_awaited();plugin.fake_client.describe.assert_not_awaited()
            self.assertIn('引用原图',event.sent[0])

    async def test_group_quote_without_optional_group_metadata(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth','allowed_group_ids':['777']})
        event=self.quote_event({'message_id':42,'message_type':'group','message':[
            {'type':'image','data':{'url':'group-image'}}]},group='777')
        await plugin.describe_command(event)
        plugin.reader.read.assert_awaited_once_with('group-image')

    async def test_adapter_error_or_timeout_never_exposes_payload(self):
        for error in [RuntimeError(SENTINEL+' '+PRIVATE_IMAGE),asyncio.TimeoutError()]:
            plugin=self.plugin_instance({'backend':'chatgpt_oauth'})
            event=self.quote_event();event.bot.call_action.side_effect=error
            with self.assertLogs('vision-test',level='WARNING') as captured:
                await plugin.describe_command(event)
            joined=' '.join(captured.output+event.sent)
            self.assertNotIn(SENTINEL,joined);self.assertNotIn(PRIVATE_IMAGE,joined)
            plugin.reader.read.assert_not_awaited()

    async def test_reply_fetch_is_bounded_and_cancelable(self):
        event=self.quote_event()
        with patch.object(self.plugin.asyncio,'wait_for',wraps=asyncio.wait_for) as bounded:
            await self.plugin.replied_image_references(event)
        self.assertEqual(bounded.call_args.args[1],10)
        event.bot.call_action.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.replied_image_references(event)

    def test_onebot_cq_and_array_images_ignore_nested_content(self):
        message='[CQ:image,file=fish.png,url=https://cdn.example.com/a?x=1&amp;y=2&#44;3]'
        self.assertEqual(self.plugin.segment_images(message),['https://cdn.example.com/a?x=1&y=2,3'])
        self.assertEqual(self.plugin.segment_images('[CQ:reply,id=5] https://private.invalid/a'),[])
        self.assertEqual(self.plugin.segment_images([{'type':'forward','data':{'message':message}}]),[])
        event=Event([self.Reply([{'type':'image','data':{'url':'dict-image'}}])])
        self.assertEqual(self.plugin.image_references(event,True),['dict-image'])

    async def test_quote_uses_existing_image_validation_and_limit(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth'})
        event=self.quote_event();plugin.reader.read.side_effect=VisionError('image_source')
        with self.assertLogs('vision-test',level='WARNING'):await plugin.describe_command(event)
        plugin.fake_client.describe.assert_not_awaited()
        self.assertIn('来源',event.sent[0])
        event=self.quote_event({'message_id':42,'sender':{'user_id':123},'message':[
            {'type':'image','data':{'url':str(i)}} for i in range(4)]})
        plugin=self.plugin_instance({'backend':'chatgpt_oauth'})
        with self.assertLogs('vision-test',level='WARNING'):await plugin.describe_command(event)
        plugin.reader.read.assert_not_awaited()

    async def test_existing_backend_does_not_intercept(self):
        plugin=self.plugin_instance({'backend':'existing','auto_reply':True})
        event=Event([self.Image('image')]);await plugin.automatic_vision(event)
        self.assertFalse(event.stopped);self.assertEqual(event.sent,[])
        plugin.fake_client.describe.assert_not_awaited()

    async def test_auto_disabled_does_not_intercept(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth','auto_reply':False})
        event=Event([self.Image('image')]);await plugin.automatic_vision(event)
        self.assertFalse(event.stopped)

    async def test_oauth_reply_once_stops_native_pipeline(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth','auto_reply':True})
        event=Event([self.Image('image')]);await plugin.automatic_vision(event);await plugin.automatic_vision(event)
        self.assertTrue(event.stopped);self.assertEqual(event.sent,['小猫在发呆。'])
        plugin.fake_client.describe.assert_awaited_once()

    async def test_group_and_user_permission_before_image_download(self):
        for group,sender,admin in [('999','123',True),('','999',False)]:
            plugin=self.plugin_instance({'backend':'chatgpt_oauth','auto_reply':True})
            event=Event([self.Image('private')],group,sender,admin);await plugin.automatic_vision(event)
            self.assertTrue(event.stopped);plugin.reader.read.assert_not_awaited()

    async def test_errors_and_logs_exclude_private_payload(self):
        plugin=self.plugin_instance({'backend':'chatgpt_oauth','auto_reply':True})
        plugin.reader.read.side_effect=RuntimeError(SENTINEL+' '+PRIVATE_IMAGE)
        event=Event([self.Image(PRIVATE_IMAGE)])
        with self.assertLogs('vision-test',level='WARNING') as captured:await plugin.automatic_vision(event)
        joined=' '.join(captured.output+event.sent)
        self.assertNotIn(SENTINEL,joined);self.assertNotIn(PRIVATE_IMAGE,joined)
        self.assertTrue(event.stopped);plugin.fake_client.describe.assert_not_awaited()


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_custom_proxy_overrides_system(self):
        with patch('vision.network.getproxies',return_value={'https':'http://system.invalid:80'}):
            self.assertEqual(choose_proxy('https://api.openai.com','http://127.0.0.1:7890'),'http://127.0.0.1:7890')

    def test_empty_uses_system_route(self):
        with patch('vision.network.proxy_bypass',return_value=False),patch('vision.network.getproxies',return_value={'https':'http://system.invalid:8080'}):
            self.assertEqual(choose_proxy('https://api.openai.com',''),'http://system.invalid:8080')

    def test_system_no_proxy_and_direct(self):
        with patch('vision.network.proxy_bypass',return_value=True):
            self.assertIsNone(choose_proxy('https://api.openai.com',''))
        with patch('vision.network.proxy_bypass',return_value=False),patch('vision.network.getproxies',return_value={}):
            self.assertIsNone(choose_proxy('https://api.openai.com',''))

    def test_unsupported_proxy_fails_privately(self):
        for value in ['socks5://127.0.0.1:1080','http://proxy.invalid/path?token='+SENTINEL,'invalid']:
            with self.assertRaises(VisionError) as error:choose_proxy('https://api.openai.com',value)
            self.assertNotIn(SENTINEL,str(error.exception))

    async def test_auth_and_model_requests_share_proxy_without_netrc(self):
        raw=types.SimpleNamespace(request=__import__('unittest.mock',fromlist=['Mock']).Mock(),close=AsyncMock(),closed=False)
        with patch('vision.network.aiohttp.ClientSession',return_value=raw) as constructor:
            async with RoutedSession('http://127.0.0.1:7890') as session:
                session.request('POST',TOKEN,data={'code':'fake'})
                session.request('GET','https://api.openai.com/v1/models')
            self.assertFalse(constructor.call_args.kwargs['trust_env'])
            self.assertTrue(all(c.kwargs['proxy']=='http://127.0.0.1:7890' for c in raw.request.call_args_list))

    async def test_image_proxy_pins_public_destination_and_keeps_host(self):
        image=Image.new('RGB',(10,10),'white');buf=BytesIO();image.save(buf,format='PNG')
        response=Response(raw=buf.getvalue());response.content_length=len(buf.getvalue())
        calls=[]
        class Route:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            def request(self,method,url,**kwargs):calls.append((str(url),kwargs));return response
        with patch('vision.images.RoutedSession',return_value=Route()) as route,patch.object(PublicResolver,'resolve',new=AsyncMock(return_value=[{'host':'93.184.216.34'}])):
            data=await ImageReader([],proxy='http://127.0.0.1:7891').download('https://cdn.example.com/a?sign=abc%2Fdef')
        self.assertEqual(data,buf.getvalue());self.assertIn('93.184.216.34',calls[0][0])
        self.assertEqual(calls[0][1]['headers']['Host'],'cdn.example.com')
        self.assertEqual(calls[0][1]['server_hostname'],'cdn.example.com')
        self.assertEqual(calls[0][1]['routing_url'],'https://cdn.example.com/a?sign=abc%2Fdef')
        self.assertEqual(route.call_args.args[0],'http://127.0.0.1:7891')


if __name__=='__main__':unittest.main(verbosity=2)

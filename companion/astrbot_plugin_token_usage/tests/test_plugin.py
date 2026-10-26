import asyncio
import importlib
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import meter
from meter.usage import Usage


def load_plugin():
    class Star:
        def __init__(self,context):pass
    api=types.ModuleType('astrbot.api');api.AstrBotConfig=dict;api.logger=logging.getLogger('usage-test')
    event=types.ModuleType('astrbot.api.event');event.AstrMessageEvent=object
    event.filter=types.SimpleNamespace(**{name:lambda *a,**k:lambda fn:fn for name in ['command','on_llm_request','on_agent_done']})
    star=types.ModuleType('astrbot.api.star');star.Star=Star;star.Context=object
    components=types.ModuleType('astrbot.api.message_components');components.Image=types.SimpleNamespace(fromBytes=lambda b:b)
    tools=types.ModuleType('astrbot.core.star.star_tools');tools.StarTools=types.SimpleNamespace(get_data_dir=lambda:tempfile.gettempdir())
    for name,mod in {'astrbot':types.ModuleType('astrbot'),'astrbot.api':api,'astrbot.api.event':event,'astrbot.api.star':star,
                     'astrbot.api.message_components':components,'astrbot.core':types.ModuleType('astrbot.core'),
                     'astrbot.core.star':types.ModuleType('astrbot.core.star'),'astrbot.core.star.star_tools':tools}.items():sys.modules[name]=mod
    package=types.ModuleType('usage_plugin');package.__path__=[str(Path(__file__).resolve().parents[1])];sys.modules['usage_plugin']=package
    sys.modules['usage_plugin.meter']=meter
    for name in ['usage','pricing','store','observer','report','card']:
        sys.modules['usage_plugin.meter.'+name]=importlib.import_module('meter.'+name)
    return importlib.import_module('usage_plugin.main')


class Event:
    def __init__(self,text='/token',scope='qq:GroupMessage:A',admin=True):
        self.text=text;self.unified_msg_origin=scope;self.admin=admin;self.sent=[];self.stopped=False
    def stop_event(self):self.stopped=True
    def get_message_str(self):return self.text
    def is_admin(self):return self.admin
    def plain_result(self,text):return text
    def chain_result(self,chain):return chain
    async def send(self,text):self.sent.append(text)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):cls.module=load_plugin()

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        context=types.SimpleNamespace(get_all_providers=lambda:[],conversation_manager=types.SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='cid')))
        with patch.object(self.module.StarTools,'get_data_dir',return_value=self.temp.name):
            self.plugin=self.module.TokenUsagePlugin(context,{'card':False,'query_access':'current_session'})
        await self.plugin.initialize();self.addAsyncCleanup(self.plugin.terminate)

    async def test_command_never_calls_llm_and_default_empty_message_is_truthful(self):
        event=Event();await self.plugin.token_command(event)
        self.assertTrue(event.stopped);self.assertIn('还没有记录',event.sent[0])

    async def test_regular_user_cannot_query_global_or_other_session(self):
        await self.plugin.ingest_external(Event(scope='qq:GroupMessage:OTHER'),request_id='one',model='model',usage={'input_tokens':100,'output_tokens':10})
        event=Event(admin=False);await self.plugin.token_command(event)
        self.assertIn('还没有记录',event.sent[0])
        event=Event('/token 累计 全部',admin=False);await self.plugin.token_command(event)
        self.assertIn('只有管理员',event.sent[0])

    async def test_admin_only_default_policy(self):
        self.plugin.config={}
        event=Event(admin=False);await self.plugin.token_command(event)
        self.assertIn('仅管理员',event.sent[0]);self.assertTrue(event.stopped)

    async def test_oauth_bridge_reports_subscription_and_deduplicates(self):
        e=Event()
        for _ in range(2):await self.plugin.ingest_external(e,request_id='same',model='model',usage={'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':0},'secret':'PRIVATE_TOKEN'})
        await self.plugin.token_command(e)
        self.assertIn('调用：1 次',e.sent[0]);self.assertIn('订阅调用 1 次',e.sent[0])
        self.assertNotIn('PRIVATE_TOKEN',e.sent[0]);self.assertIn('0.0%',e.sent[0])

    async def test_query_uses_configured_prices_only_for_api(self):
        self.plugin.config['prices']=[dict(provider='ChatGPT OAuth',model='model',billing='api',currency='CNY',input='1',cached='1',output='1')]
        e=Event();await self.plugin.ingest_external(e,request_id='one',model='model',usage={'input_tokens':100,'output_tokens':10})
        await self.plugin.token_command(e)
        self.assertNotIn('¥',e.sent[0]);self.assertIn('订阅用量',e.sent[0])

    async def test_current_conversation_and_model_filter(self):
        e=Event('/token 当前会话 模型 model 文本')
        await self.plugin.ingest_external(e,request_id='one',model='model',usage={'input_tokens':100,'output_tokens':10})
        await self.plugin.token_command(e)
        self.assertIn('调用：1 次',e.sent[0])
        self.plugin.context.conversation_manager.get_curr_conversation_id.return_value='new-cid'
        e.sent=[];await self.plugin.token_command(e);self.assertIn('还没有记录',e.sent[0])

    async def test_unknown_conversation_does_not_merge_all_conversations(self):
        self.plugin.context.conversation_manager.get_curr_conversation_id.return_value=''
        e=Event('/token 当前会话');await self.plugin.token_command(e)
        self.assertIn('ID不可用',e.sent[0])

    async def test_render_failure_falls_back_to_text(self):
        self.plugin.config['card']=True
        e=Event();await self.plugin.ingest_external(e,request_id='one',model='model',usage={})
        with patch.object(self.module,'render',side_effect=RuntimeError('font missing')):await self.plugin.token_command(e)
        self.assertIsInstance(e.sent[0],str);self.assertIn('未知',e.sent[0])

    async def test_record_failure_logs_no_private_payload(self):
        with patch.object(self.plugin.store,'add',side_effect=RuntimeError('PRIVATE_TOKEN_8387 PRIVATE_IMAGE_8288')):
            with self.assertLogs('usage-test',level='WARNING') as logs:
                await self.plugin.ingest_external(Event(),request_id='one',model='model',usage={})
                await self.plugin.queue.join()
        self.assertEqual(self.plugin.dropped,1)
        self.assertNotIn('PRIVATE_',str(logs.output))

    async def test_shutdown_drains_queue_and_disabled_plugin_records_nothing(self):
        await self.plugin.ingest_external(Event(),request_id='one',model='model',usage={})
        await self.plugin.terminate()
        self.assertEqual(self.plugin.store.report()['calls'],1)
        await self.plugin.ingest_external(Event(),request_id='two',model='model',usage={})
        self.assertEqual(self.plugin.store.report()['calls'],1)

import asyncio
import json
import sqlite3
import tempfile
import time
import types
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from meter.usage import Usage, parse_usage, response_usage, safe_name
from meter.pricing import estimate
from meter.store import Store
from meter.observer import Observer, active_call, scope_context
from meter.report import SHANGHAI, metrics, money, period_start, plain


class UsageTests(unittest.TestCase):
    def test_responses_real_zero_and_missing_cache_are_distinct(self):
        known=parse_usage({'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':0}})
        missing=parse_usage({'input_tokens':100,'output_tokens':10})
        self.assertEqual((known.input,known.output,known.cached),(100,10,0))
        self.assertIsNone(missing.cached)
        self.assertIsNone(parse_usage(None).input)

    def test_deepseek_and_chat(self):
        self.assertEqual(parse_usage({'prompt_tokens':100,'completion_tokens':20,'prompt_cache_hit_tokens':80}),Usage(100,20,80))
        self.assertEqual(parse_usage({'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':50}}),Usage(100,20,50))

    def test_anthropic_split_input_and_write(self):
        u=parse_usage({'input_tokens':10,'output_tokens':20,'cache_read_input_tokens':30,'cache_creation_input_tokens':40})
        self.assertEqual(u,Usage(80,20,30,40))
        missing=parse_usage({'input_tokens':10,'output_tokens':20,'cache_read_input_tokens':30})
        self.assertIsNone(missing.input)

    def test_gemini_reasoning_is_included_once(self):
        self.assertEqual(parse_usage({'prompt_token_count':100,'candidates_token_count':20,'thoughts_token_count':5,'cached_content_token_count':40}),Usage(100,25,40))

    def test_astrbot_zero_cache_is_not_evidence_of_cache_reporting(self):
        self.assertEqual(parse_usage(types.SimpleNamespace(input_other=10,input_cached=0,output=5)),Usage(10,5,None))
        self.assertEqual(parse_usage(types.SimpleNamespace(input_other=10,input_cached=5,output=5)),Usage(15,5,5))

    def test_invalid_counters_are_unknown(self):
        for value in [-1, True, 2.5, '20', 10**12]:
            self.assertIsNone(Usage(value,0).input)
        self.assertIsNone(Usage(10,5,20).cached)
        self.assertIsNone(Usage(10,5,8,8).cached)

    def test_raw_is_preferred_over_normalized_default(self):
        r=types.SimpleNamespace(raw_completion={'usage':{'input_tokens':10,'output_tokens':3}},
                                usage=types.SimpleNamespace(input_other=10,input_cached=0,output=3))
        self.assertEqual(response_usage(r),Usage(10,3,None))

    def test_unrecognized_usage_and_sensitive_metadata_are_not_copied(self):
        self.assertEqual(parse_usage({'private':'TOKEN_SECRET','total_tokens':44}),Usage())
        for name in ['https://host.invalid/?secret=1','Bearer token','sk-secret','ghp_secret','a\nb']:
            self.assertEqual(safe_name(name),'未知')


class PriceTests(unittest.TestCase):
    def price(self,**kwargs):
        return dict(model='model',provider='p',billing='api',currency='CNY',input='2',cached='0.2',output='8',**kwargs)

    def test_actual_cache_is_not_double_counted(self):
        p=self.price();r=estimate(Usage(1000000,100000,800000),'unknown','p','model',[p])
        self.assertEqual(Decimal(r['cost']),Decimal('1.36'))
        self.assertEqual(Decimal(r['saving']),Decimal('1.44'))

    def test_write_cache_requires_explicit_price(self):
        p=self.price()
        self.assertIsNone(estimate(Usage(100,10,20,30),'api','p','model',[p])['cost'])
        p['write']='3'
        self.assertEqual(Decimal(estimate(Usage(100,10,20,30),'api','p','model',[p])['cost']),Decimal('0.000274'))

    def test_unknown_cache_can_only_use_equal_input_rates(self):
        p=self.price()
        self.assertIsNone(estimate(Usage(100,10,None),'api','p','model',[p])['cost'])
        p['cached']='2'
        self.assertEqual(Decimal(estimate(Usage(100,10,None),'api','p','model',[p])['cost']),Decimal('0.00028'))

    def test_subscription_cannot_be_priced_as_api(self):
        self.assertIsNone(estimate(Usage(100,10,20),'subscription','p','model',[self.price()])['cost'])
        self.assertIsNone(estimate(Usage(100,10,20),'local','p','model',[self.price()])['cost'])

    def test_missing_invalid_or_unmatched_prices_are_unknown(self):
        for p in [{},dict(self.price(),input='nan'),dict(self.price(),output='-1'),dict(self.price(),currency='BAD'),dict(self.price(),model='other')]:
            self.assertIsNone(estimate(Usage(100,10,20),'api','p','model',[p])['cost'])

    def test_provider_specific_price_wins(self):
        a=dict(self.price(),provider='',input='100');b=self.price()
        self.assertEqual(estimate(Usage(100,10,20),'api','p','model',[a,b])['cost'],estimate(Usage(100,10,20),'api','p','model',[b])['cost'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.serial=0

    def add(self,usage=Usage(100,10,20),**kwargs):
        self.serial+=1
        row=dict(request_id=str(self.serial),scope='PRIVATE_SCOPE_95708',conversation='PRIVATE_CID_68403',
                 provider='p',model='m',usage=usage)
        row.update(kwargs);return self.store.add(**row)

    def test_duplicate_report_counts_once_but_separate_calls_count_twice(self):
        self.add(request_id='same');self.add(request_id='same');self.add(request_id='new')
        self.assertEqual(self.store.report()['calls'],2)

    def test_scope_and_conversation_isolation(self):
        self.add();self.add(scope='other',conversation='other');self.add(conversation='next')
        self.assertEqual(self.store.report(scope='PRIVATE_SCOPE_95708')['calls'],2)
        self.assertEqual(self.store.report(scope='PRIVATE_SCOPE_95708',conversation='PRIVATE_CID_68403')['calls'],1)

    def test_weighted_cache_ratio_and_missing_coverage(self):
        self.add(Usage(100,10,80));self.add(Usage(900,20,90));self.add(Usage(500,10,None));self.add(Usage())
        r=self.store.report()
        self.assertEqual(r['cache_ratio'],0.17);self.assertEqual(r['cache_known'],2)
        self.assertEqual(metrics(r)[0],'≥1,500')
        self.assertIn('缓存 2/4',plain(r,'今日','测试'))

    def test_subscription_mix_does_not_appear_as_all_free(self):
        self.add(billing='subscription');self.add()
        r=self.store.report();self.assertEqual(r['subscription'],1)
        self.assertEqual(metrics(r)[3],'未配置 / 未知')

    def test_no_plain_scope_ids_prompts_or_tokens_on_disk(self):
        self.add(parse_usage({'input_tokens':10,'output_tokens':2,'prompt':'PRIVATE_PROMPT_82319','token':'PRIVATE_TOKEN_62419'}))
        content=self.store.path.read_bytes()
        for value in [b'PRIVATE_SCOPE_95708',b'PRIVATE_CID_68403',b'PRIVATE_PROMPT_82319',b'PRIVATE_TOKEN_62419']:
            self.assertNotIn(value,content)

    def test_prices_snapshot_and_currency_separation(self):
        p=dict(model='m',provider='p',billing='api',currency='CNY',input='0.1',cached='0.1',output='0')
        self.add(Usage(1000000,0,0),prices=[p]);p['input']='0.2';p['cached']='0.2'
        self.add(Usage(1000000,0,0),prices=[p]);p['currency']='USD'
        self.add(Usage(1000000,0,0),prices=[p]);r=self.store.report()
        self.assertEqual(r['costs'],{'CNY':Decimal('0.3'),'USD':Decimal('0.2')})
        self.assertEqual(r['priced'],3)

    def test_failure_without_usage_is_recorded_as_unknown(self):
        self.add(Usage(),status='failed');r=self.store.report()
        self.assertEqual(r['failed'],1);self.assertEqual(metrics(r)[0],'未知')

    def test_retention_count_and_model_filters(self):
        self.add(timestamp=time.time()-100*86400);self.assertEqual(self.store.report()['calls'],0)
        self.add();self.add(model='other');self.assertEqual(self.store.report(model='other')['calls'],1)
        small=Store(Path(self.temp.name)/'limited',max_records=100)
        for i in range(110):small.add(request_id=str(i),scope='',conversation='',provider='p',model='m',usage=Usage())
        self.assertLessEqual(small.report()['calls'],100)

    def test_restart_preserves_hash_key_and_history(self):
        self.add();again=Store(self.temp.name)
        self.assertEqual(again.report(scope='PRIVATE_SCOPE_95708')['calls'],1)


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    def provider(self):
        class Provider:
            def meta(self):return types.SimpleNamespace(id='p')
            def get_model(self):return 'configured'
            async def text_chat(self,**kwargs):
                return types.SimpleNamespace(role='assistant',raw_completion={'model':'actual','usage':{'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':30}}})
            async def text_chat_stream(self,**kwargs):
                yield types.SimpleNamespace(role='assistant',raw_completion={'usage':{'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':20}}})
                yield types.SimpleNamespace(role='assistant',raw_completion={'usage':{'input_tokens':100,'output_tokens':20,'input_tokens_details':{'cached_tokens':20}}})
        return Provider()

    async def asyncSetUp(self):
        self.records=[]
        async def emit(row):self.records.append(row)
        self.observer=Observer(emit);self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        self.observer.restore();scope_context.set(None);active_call.set(False)

    async def test_normal_call_is_unchanged_and_actual_model_is_recorded(self):
        p=self.provider();self.observer.install([p]);r=await p.text_chat(prompt='PRIVATE_PROMPT')
        self.assertEqual(r.raw_completion['model'],'actual')
        self.assertEqual(self.records[0]['model'],'actual');self.assertEqual(self.records[0]['usage'],Usage(100,20,30))
        self.assertNotIn('PRIVATE_PROMPT',str(self.records))

    async def test_stream_uses_last_aggregate_not_sum(self):
        p=self.provider();self.observer.install([p]);parts=[r async for r in p.text_chat_stream()]
        self.assertEqual(len(parts),2);self.assertEqual(len(self.records),1)
        self.assertEqual(self.records[0]['usage'],Usage(100,20,20))

    async def test_nested_plain_stream_is_not_double_counted(self):
        p=self.provider()
        async def plain_call(**kwargs):
            async for result in p.text_chat_stream():pass
            return result
        p.text_chat=plain_call;self.observer.install([p]);await p.text_chat()
        self.assertEqual(len(self.records),1)

    async def test_close_mid_stream_and_next_call_do_not_leak_context(self):
        p=self.provider();self.observer.install([p]);gen=p.text_chat_stream();await anext(gen)
        self.assertFalse(active_call.get());await gen.aclose();await p.text_chat()
        self.assertEqual([r['status'] for r in self.records],['interrupted','completed'])

    async def test_failure_and_cancel_are_not_swallowed(self):
        p=self.provider()
        async def fail(**kwargs):raise RuntimeError('PRIVATE_TOKEN_74623')
        p.text_chat=fail;self.observer.install([p])
        with self.assertRaises(RuntimeError):await p.text_chat()
        self.assertEqual(self.records[0]['status'],'failed');self.assertNotIn('PRIVATE_TOKEN',str(self.records))
        self.observer.restore();self.records.clear();self.observer.closed=False
        async def cancel(**kwargs):raise asyncio.CancelledError()
        p.text_chat=cancel;self.observer.install([p])
        with self.assertRaises(asyncio.CancelledError):await p.text_chat()
        self.assertEqual(self.records[0]['status'],'interrupted')

    async def test_observer_failure_cannot_break_provider(self):
        p=self.provider()
        async def fail(_):raise RuntimeError('private')
        o=Observer(fail);o.install([p]);self.addCleanup(o.restore)
        self.assertIsNotNone(await p.text_chat());self.assertEqual(o.errors,1)

    async def test_concurrent_scope_and_repeated_install(self):
        p=self.provider();self.observer.install([p]);self.observer.install([p])
        async def call(scope):
            scope_context.set({'scope':scope,'conversation':scope});await p.text_chat()
        await asyncio.gather(call('one'),call('two'))
        self.assertEqual({r['scope'] for r in self.records},{'one','two'});self.assertEqual(len(self.records),2)

    async def test_restore_does_not_remove_later_third_party_wrapper(self):
        p=self.provider();original=p.text_chat;self.observer.install([p]);observed=p.text_chat
        async def later(**kwargs):return await observed(**kwargs)
        p.text_chat=later;self.observer.restore();await p.text_chat()
        self.assertIs(p.text_chat,later);self.assertEqual(self.records,[])


class ReportTests(unittest.TestCase):
    def test_calendar_day_and_week_boundary_in_shanghai(self):
        dt=datetime(2026,10,26,13,45,tzinfo=SHANGHAI)
        self.assertEqual(period_start('今日',dt),datetime(2026,10,26,tzinfo=SHANGHAI).timestamp())
        self.assertEqual(period_start('近7天',dt),datetime(2026,10,20,tzinfo=SHANGHAI).timestamp())
        self.assertEqual(period_start('累计',dt),0)

    def test_small_cost_is_not_rounded_to_free(self):
        self.assertIn('<0.000001',money({'CNY':Decimal('0.00000001')}))

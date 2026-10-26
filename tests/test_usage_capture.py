import unittest
from vision.client import capture_usage, read_answer


class UsageCaptureTests(unittest.TestCase):
    def test_only_numeric_whitelisted_counters_are_copied(self):
        meter={}
        capture_usage({'model':'model','id':'PRIVATE_ID','usage':{
            'input_tokens':100,'output_tokens':20,'token':'PRIVATE_TOKEN',
            'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':10,'private':'PRIVATE_IMAGE'}}},meter)
        self.assertEqual(meter,{'model':'model','usage':{'input_tokens':100,'output_tokens':20,
                           'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':10}}})
        self.assertNotIn('PRIVATE',str(meter))

    def test_missing_usage_is_not_filled_with_zero(self):
        meter={};capture_usage({'usage':None},meter);self.assertNotIn('usage',meter)
        capture_usage({'usage':{'input_tokens':-1,'output_tokens':True,'input_tokens_details':{'cached_tokens':'0'}}},meter)
        self.assertEqual(meter,{'usage':{}})

    def test_diagnostic_sink_remains_separate(self):
        capture_usage({'usage':{'input_tokens':10}},None)


class UsageStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_usage_is_reported_without_diagnostics_or_answer_changes(self):
        import json
        event={'type':'response.completed','response':{'model':'model','status':'completed',
               'usage':{'input_tokens':100,'output_tokens':20,'input_tokens_details':{'cached_tokens':30}},
               'output':[{'type':'message','content':[{'type':'output_text','text':'answer'}]}]}}
        class Content:
            async def iter_chunked(self,size):yield b'data: '+json.dumps(event).encode()+b'\n\n'
        meter={};diagnostic={}
        self.assertEqual(await read_answer(Content(),diagnostic,meter),'answer')
        self.assertEqual(meter['usage']['input_tokens'],100)
        self.assertNotIn('usage',diagnostic)

"""Publication safety tests with a fake GitHub transport; never access network."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import publish_releases as publisher


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'release-assets').mkdir()
        (self.root / 'release-notes').mkdir()
        self.plan = []
        for n in range(2):
            tag = f'v0.1.{n}-test'
            name = tag + '.zip'
            item = {'tag': tag, 'title': tag, 'asset': name, 'commit_subject': 'snapshot ' + str(n)}
            self.plan.append(item)
            (self.root / 'release-assets' / name).write_bytes(b'fake zip')
            (self.root / 'release-assets' / (name + '.sha256')).write_bytes(b'fake checksum')
            (self.root / 'release-notes' / (tag + '.md')).write_text('test notes')
        (self.root / 'release-plan.json').write_text(json.dumps(self.plan))
        self.calls = []

    def transport(self, existing_tag=False):
        owner = self
        class Transport:
            def open(self, req, timeout):
                owner.calls.append((req.method, req.full_url, req.data))
                assert req.full_url.startswith(('https://api.github.com/', 'https://uploads.github.com/'))
                if req.method == 'GET':
                    if '/assets' in req.full_url:
                        return io.BytesIO(b'[]')
                    if existing_tag and '/git/ref/tags/' in req.full_url:
                        return io.BytesIO(b'{"object":{"sha":"wrong-commit"}}')
                    raise urllib.error.HTTPError(req.full_url, 404, 'Not found', {}, None)
                if '/releases' in req.full_url and req.method == 'POST' and req.full_url.startswith('https://api.'):
                    payload = {'id': len(owner.calls), 'draft': True, 'upload_url': 'https://uploads.github.com/assets{?name}', 'html_url': 'https://github.com/example/release'}
                elif req.full_url.startswith('https://uploads.'):
                    payload = {'size': len(req.data)}
                else:
                    payload = {'html_url': 'https://github.com/example/release'}
                return io.BytesIO(json.dumps(payload).encode())
        return Transport()

    def invoke(self, existing_tag=False):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_REPOSITORY': 'example/test', 'GH_TOKEN': 'fake-unit-test-token'}
        with patch.dict(os.environ, env), patch.object(publisher, 'ROOT', self.root), \
             patch.object(publisher, 'verify'), \
             patch.object(publisher.subprocess, 'check_output', return_value='aaa\0snapshot 0\nbbb\0snapshot 1\n'), \
             patch.object(publisher.urllib.request, 'build_opener', return_value=self.transport(existing_tag)):
            publisher.main()

    def test_release_order_and_attach_before_publish(self):
        self.invoke()
        creates = [json.loads(body) for method, url, body in self.calls
                   if method == 'POST' and url.endswith('/releases')]
        self.assertEqual([x['tag_name'] for x in creates], ['v0.1.0-test', 'v0.1.1-test'])
        self.assertTrue(all(x['draft'] and x['prerelease'] for x in creates))
        uploads = 0
        for method, url, _ in self.calls:
            if url.startswith('https://uploads.'):
                uploads += 1
            if method == 'PATCH':
                self.assertEqual(uploads, 2)
                uploads = 0

    def test_conflicting_tag_stops_without_mutation(self):
        with self.assertRaisesRegex(RuntimeError, 'Existing tag'):
            self.invoke(existing_tag=True)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls))

    def test_local_or_pr_invocation_cannot_publish(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false', 'GITHUB_REF': 'refs/heads/main'}), \
             patch.object(publisher.urllib.request, 'build_opener') as transport:
            with self.assertRaises(SystemExit):
                publisher.main()
            transport.assert_not_called()
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/pull/1/merge'}):
            with self.assertRaises(SystemExit):
                publisher.main()

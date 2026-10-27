"""Offline integrity checks; no accounts, network or private images."""
import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def verify():
    plan = json.loads((ROOT / 'release-plan.json').read_text(encoding='utf-8'))
    assert [p['tag'] for p in plan] == [f'v0.1.{i}-test' for i in range(6)] + ['v0.1.5-fix1', 'v0.1.5-alpha1']
    items = []
    for release in plan:
        assert (ROOT / 'release-notes' / (release['tag'] + '.md')).is_file()
        assert not release.get('extra_assets'), 'OAuth releases must not bundle other plugins'
        items.append(dict(release, verify_source=release is plan[-1]))
    for item in items:
        archive = ROOT / 'release-assets' / item['asset']
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        assert digest == item['sha256']
        assert digest == archive.with_suffix('.zip.sha256').read_text().split()[0]
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            names = z.namelist()
            assert len(names) == len(set(names))
            prefix = item.get('plugin', 'astrbot_plugin_chatgpt_vision') + '/'
            for name in names:
                path = PurePosixPath(name)
                assert name.startswith(prefix) and '..' not in path.parts and not path.is_absolute()
                assert path.suffix not in {'.bin', '.pem', '.key', '.pyc', '.lock'}
                assert path.name not in {'.env', 'auth.json'}
                assert not re.search(rb'(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_-]{20,}', z.read(name))
            metadata = z.read(prefix + 'metadata.yaml').decode('utf-8-sig')
            assert 'version: v' + item['version'] + '\n' in metadata
            manifest = z.read(prefix + 'MANIFEST.sha256').decode('utf-8-sig')
            covered = set()
            for line in manifest.splitlines():
                expected, rel = line.split('  ', 1)
                assert hashlib.sha256(z.read(prefix + rel)).hexdigest() == expected
                covered.add(prefix + rel)
            assert covered == set(names) - {prefix + 'MANIFEST.sha256'}
            if item.get('verify_source'):
                source = ROOT if prefix == 'astrbot_plugin_chatgpt_vision/' else ROOT / 'companion' / item['plugin']
                for name in covered:
                    data = (source / name[len(prefix):]).read_bytes()
                    if not name.endswith('.png'):
                        data = data.replace(b'\r\n', b'\n')
                    assert z.read(name) == data, 'Archive differs from source: ' + name
            if item['version'].startswith('0.1.5'):
                assert hashlib.sha256(z.read(prefix + 'logo.png')).hexdigest() == 'c035c48b8b86e676d9ca69481bf99d9c4d03f5e4435f7571ef8667f1fb751da6'
        print(item['asset'] + ': archive, manifest and notes OK')


if __name__ == '__main__':
    verify()

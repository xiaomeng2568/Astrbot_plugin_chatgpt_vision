"""Offline integrity checks; no accounts, network or private images."""
import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def verify():
    plan = json.loads((ROOT / 'release-plan.json').read_text(encoding='utf-8'))
    assert [p['version'] for p in plan] == ['0.1.0', '0.1.1', '0.1.2', '0.1.3', '0.1.4']
    for item in plan:
        archive = ROOT / 'release-assets' / item['asset']
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        assert digest == item['sha256']
        assert digest == archive.with_suffix('.zip.sha256').read_text().split()[0]
        assert (ROOT / 'release-notes' / (item['tag'] + '.md')).is_file()
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            names = z.namelist()
            assert len(names) == len(set(names))
            prefix = 'astrbot_plugin_chatgpt_vision/'
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
        print(item['tag'] + ': archive, manifest and notes OK')


if __name__ == '__main__':
    verify()

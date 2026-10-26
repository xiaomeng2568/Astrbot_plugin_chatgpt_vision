"""Build the repaired v0.1.5 OAuth archive; statistics is published separately."""
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package(source, plugin, version, paths, revision='-test'):
    files = {}
    for relative in paths:
        path = source / relative
        candidates = path.rglob('*') if path.is_dir() else [path]
        for file in candidates:
            if file.is_file() and '__pycache__' not in file.parts and file.suffix != '.pyc':
                data = file.read_bytes()
                # Normalize text across Windows and Linux; retain PNG bytes exactly.
                if file.suffix != '.png':
                    data = data.replace(b'\r\n', b'\n')
                files[file.relative_to(source).as_posix()] = data
    manifest = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n'
                       for name, data in sorted(files.items())).encode()
    files['MANIFEST.sha256'] = manifest
    name = f'{plugin}-v{version}{revision}.zip'
    archive = ROOT / 'release-assets' / name
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for relative, data in sorted(files.items()):
            info = zipfile.ZipInfo(plugin + '/' + relative, (2026, 10, 26, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            output.writestr(info, data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (archive.parent / (name + '.sha256')).write_text(digest + '  ' + name + '\n', encoding='utf-8', newline='\n')
    return {'version': version, 'asset': name, 'sha256': digest, 'plugin': plugin}


def main():
    vision = package(ROOT, 'astrbot_plugin_chatgpt_vision', '0.1.5', [
        '__init__.py', '_conf_schema.json', '.gitignore', 'LICENSE', 'README.md',
        'IMPLEMENTATION_PLAN.md', 'main.py', 'manage.py', 'metadata.yaml', 'requirements.txt',
        'test-results.txt', '一键自检.cmd', '安全与验收说明.md', '更新说明.txt', 'logo.png',
        'vision', 'tests/test_vision.py', 'tests/test_usage_capture.py'], revision='-fix1')
    plan_path = ROOT / 'release-plan.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    entry = dict(vision, tag='v0.1.5-fix1', title='v0.1.5 修正版 · 数据目录隔离',
                 commit_subject='修复 v0.1.5 插件数据目录隔离')
    plan = [item for item in plan if item['tag'] != 'v0.1.5-fix1'] + [entry]
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps(entry, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

"""GitHub Actions only: publish exact original archives in chronological order."""
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from verify_archives import verify

ROOT = Path(__file__).resolve().parents[1]
API = 'https://api.github.com'


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise SystemExit('Publication is restricted to GitHub Actions on main.')
    verify()
    repo = os.environ['GITHUB_REPOSITORY']
    token = os.environ['GH_TOKEN']
    plan = json.loads((ROOT / 'release-plan.json').read_text(encoding='utf-8'))

    def request(method, path, payload=None, binary=None):
        url = path if path.startswith('https://uploads.github.com/') else API + '/repos/' + repo + path
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                   'User-Agent': 'astrbot-vision-release', 'X-GitHub-Api-Version': '2022-11-28'}
        if binary is not None:
            body = binary
            headers['Content-Type'] = 'application/octet-stream'
        elif payload is not None:
            body = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        else:
            body = None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        # Never forward an authenticated request to a redirected host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and method == 'GET':
                return None
            raise RuntimeError('GitHub operation failed: HTTP ' + str(error.code)) from None

    history = subprocess.check_output(['git', 'log', '--all', '--format=%H%x00%s'], cwd=ROOT, text=True)
    for item in plan:
        matches = [line.split('\0', 1)[0] for line in history.splitlines()
                   if line.partition('\0')[2] == item['commit_subject']]
        if len(set(matches)) != 1:
            raise RuntimeError('Missing or ambiguous historical source commit: ' + item['tag'])
        commit = matches[0]
        tag = item['tag']
        ref = request('GET', '/git/ref/tags/' + tag)
        if ref is None:
            request('POST', '/git/refs', {'ref': 'refs/tags/' + tag, 'sha': commit})
        elif ref['object']['sha'] != commit:
            raise RuntimeError('Existing tag does not match historical source commit: ' + tag)
        release = request('GET', '/releases/tags/' + tag)
        if release is None:
            release = request('POST', '/releases', {
                'tag_name': tag, 'target_commitish': commit, 'name': item['title'],
                'body': (ROOT / 'release-notes' / (tag + '.md')).read_text(encoding='utf-8'),
                'draft': True, 'prerelease': True, 'make_latest': 'false'})
        assets = {a['name']: a for a in request('GET', '/releases/' + str(release['id']) + '/assets')}
        names = [item['asset']] + [extra['asset'] for extra in item.get('extra_assets', [])]
        for name in [filename for name in names for filename in (name, name + '.sha256')]:
            data = (ROOT / 'release-assets' / name).read_bytes()
            digest = 'sha256:' + hashlib.sha256(data).hexdigest()
            if name in assets:
                if assets[name].get('digest') != digest:
                    raise RuntimeError('Existing asset cannot be verified; no overwrite performed: ' + name)
                continue
            upload = release['upload_url'].split('{', 1)[0] + '?name=' + urllib.parse.quote(name)
            created = request('POST', upload, binary=data)
            if created['size'] != len(data) or created.get('digest', digest) != digest:
                raise RuntimeError('Uploaded asset mismatch: ' + name)
        if release['draft']:
            release = request('PATCH', '/releases/' + str(release['id']), {'draft': False})
        print('Published: ' + release['html_url'])


if __name__ == '__main__':
    main()

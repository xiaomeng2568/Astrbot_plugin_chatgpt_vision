"""App-owned SQLite, HMAC conversation IDs, immutable price snapshots."""
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from .usage import Usage, safe_name
from .pricing import estimate


class Store:
    def __init__(self, directory, retention_days=90, max_records=100000):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'usage.sqlite3'
        self.lock = threading.RLock()
        self.retention = max(1, min(int(retention_days), 3650)) * 86400
        self.max_records = max(100, min(int(max_records), 100000))
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('scope_key', secrets.token_hex(32)))
            self.key = bytes.fromhex(db.execute('SELECT value FROM settings WHERE key=?', ('scope_key',)).fetchone()[0])
            db.execute('''CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, ts REAL NOT NULL, scope TEXT NOT NULL, conversation TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                input INTEGER, output INTEGER, cached INTEGER, written INTEGER,
                billing TEXT NOT NULL, currency TEXT NOT NULL, cost TEXT, saving TEXT)''')
            db.execute('CREATE INDEX IF NOT EXISTS calls_scope_time ON calls(scope,ts)')
            db.execute('CREATE INDEX IF NOT EXISTS calls_time ON calls(ts)')
        if os.name != 'nt':
            os.chmod(self.directory, 0o700)
            os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def identity(self, value):
        return hmac.new(self.key, str(value).encode(), hashlib.sha256).hexdigest()

    def add(self, *, request_id, scope, conversation, provider, model, usage, source='provider',
            status='completed', billing='unknown', prices=(), timestamp=None):
        if not isinstance(usage, Usage):
            raise ValueError('Numeric Usage required')
        ts = time.time() if timestamp is None else float(timestamp)
        provider, model = safe_name(provider), safe_name(model)
        price = estimate(usage, billing, provider, model, prices)
        status = status if status in {'completed', 'failed', 'interrupted'} else 'failed'
        source = source if source in {'provider', 'chatgpt_oauth'} else 'provider'
        row = (self.identity(request_id), ts, self.identity(scope), self.identity(conversation) if conversation else '',
               provider, model, source, status, usage.input, usage.output, usage.cached, usage.written,
               price['billing'], price['currency'], price['cost'], price['saving'])
        with self.lock, self.connect() as db:
            changed = db.execute('INSERT OR IGNORE INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', row).rowcount
            db.execute('DELETE FROM calls WHERE ts < ?', (time.time() - self.retention,))
            count = db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]
            if count > self.max_records:
                batch = max(count - self.max_records, max(1, self.max_records // 20))
                db.execute('DELETE FROM calls WHERE rowid IN (SELECT rowid FROM calls ORDER BY ts LIMIT ?)', (batch,))
        return bool(changed)

    def report(self, *, since=0, scope=None, conversation=None, model=None):
        where, params = ['ts>=?'], [max(since, time.time() - self.retention)]
        if scope is not None:
            where.append('scope=?'); params.append(self.identity(scope))
        if conversation is not None:
            where.append('conversation=?'); params.append(self.identity(conversation))
        if model:
            where.append('model=?'); params.append(model)
        with self.lock, self.connect() as db:
            rows = db.execute('SELECT * FROM calls WHERE ' + ' AND '.join(where) + ' ORDER BY ts', params).fetchall()
        result = dict(calls=len(rows), input=0, output=0, cached=0, cache_input=0,
                      input_known=0, output_known=0, cache_known=0, failed=0,
                      subscription=0, local=0, priced=0, costs={}, savings={}, groups=[])
        groups = {}
        for r in rows:
            result['failed'] += r['status'] != 'completed'
            for name in ('input', 'output'):
                if r[name] is not None:
                    result[name] += r[name]; result[name + '_known'] += 1
            if r['cached'] is not None and r['input'] is not None:
                result['cached'] += r['cached']; result['cache_input'] += r['input']; result['cache_known'] += 1
            result['subscription'] += r['billing'] == 'subscription'
            result['local'] += r['billing'] == 'local'
            if r['cost'] is not None:
                c = r['currency']; result['priced'] += 1
                result['costs'][c] = result['costs'].get(c, Decimal(0)) + Decimal(r['cost'])
                if r['saving'] is not None:
                    result['savings'][c] = result['savings'].get(c, Decimal(0)) + Decimal(r['saving'])
            key = (r['provider'], r['model'])
            g = groups.setdefault(key, dict(provider=key[0], model=key[1], calls=0, input=0, output=0, known=0))
            g['calls'] += 1
            g['input'] += r['input'] or 0; g['output'] += r['output'] or 0
            g['known'] += r['input'] is not None and r['output'] is not None
        result['groups'] = sorted(groups.values(), key=lambda g: g['input'] + g['output'], reverse=True)
        result['first_ts'] = rows[0]['ts'] if rows else None
        result['cache_ratio'] = result['cached'] / result['cache_input'] if result['cache_input'] else None
        return result

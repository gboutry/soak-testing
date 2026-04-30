from __future__ import annotations


def controller_cloud_init() -> str:
    return r"""#!/bin/bash
set -eux
cat >/usr/local/bin/soak-controller <<'PY'
#!/usr/bin/env python3
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path('/var/lib/soak-tester')
ROOT.mkdir(parents=True, exist_ok=True)
HEARTBEATS = ROOT / 'heartbeats.jsonl'
EVENTS = ROOT / 'events.jsonl'
METRICS = ROOT / 'metrics.jsonl'
CONSOLE = Path('/dev/console')
DEBUG_CONSOLE = os.environ.get('SOAK_CONTROLLER_DEBUG_CONSOLE') == '1'


def mirror(kind, payload):
    if not DEBUG_CONSOLE:
        return
    try:
        with CONSOLE.open('a') as stream:
            stream.write('SOAK_' + kind.upper() + ' ' + payload + '\n')
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get('content-length', '0'))
        payload = self.rfile.read(length).decode()
        if self.path == '/heartbeat':
            records = [payload]
            target = HEARTBEATS
            kind = 'heartbeat'
        elif self.path == '/metrics':
            parsed = json.loads(payload)
            items = parsed if isinstance(parsed, list) else [parsed]
            records = [json.dumps(item) for item in items]
            target = METRICS
            kind = 'metric'
        else:
            parsed = json.loads(payload)
            items = parsed if isinstance(parsed, list) else [parsed]
            records = [json.dumps(item) for item in items]
            target = EVENTS
            kind = 'event'
        with target.open('a') as stream:
            for record in records:
                stream.write(record + '\n')
                mirror(kind, record)
        self._json(200, {'ok': True})

    def do_GET(self):
        if self.path == '/status':
            heartbeats = HEARTBEATS.read_text().splitlines()[-200:] if HEARTBEATS.exists() else []
            self._json(200, {'heartbeats': [json.loads(x) for x in heartbeats if x]})
        elif self.path == '/events':
            events = EVENTS.read_text().splitlines()[-200:] if EVENTS.exists() else []
            self._json(200, {'events': [json.loads(x) for x in events if x]})
        elif self.path == '/metrics':
            metrics = METRICS.read_text().splitlines()[-1000:] if METRICS.exists() else []
            self._json(200, {'metrics': [json.loads(x) for x in metrics if x]})
        else:
            self._json(404, {'error': 'not found'})


server = ThreadingHTTPServer(('0.0.0.0', 8080), Handler)
server.daemon_threads = True
server.serve_forever()
PY
chmod 0755 /usr/local/bin/soak-controller
nohup /usr/local/bin/soak-controller >/var/log/soak-controller.log 2>&1 &
"""

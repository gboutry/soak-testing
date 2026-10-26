from __future__ import annotations


def controller_cloud_init(*, token: str = "", port: int = 8080) -> str:
    script = r"""#!/bin/bash
set -eux
cat >/usr/local/bin/soak-controller <<'PY'
#!/usr/bin/env python3
import json
import hmac
import time
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path('/var/lib/soak-tester')
ROOT.mkdir(parents=True, exist_ok=True)
HEARTBEATS = ROOT / 'heartbeats.jsonl'
EVENTS = ROOT / 'events.jsonl'
METRICS = ROOT / 'metrics.jsonl'
CONSOLE = Path('/dev/console')
DEBUG_CONSOLE = os.environ.get('SOAK_CONTROLLER_DEBUG_CONSOLE') == '1'
LATEST_HEARTBEATS = {}
CONTROL_TOKEN = __SOAK_CONTROL_TOKEN__
CONTROL = {"started_at": None, "ends_at": None}
CONTROL_LOCK = threading.Lock()
HEARTBEAT_LOCK = threading.Lock()


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

    def _authorized(self):
        return bool(CONTROL_TOKEN) and hmac.compare_digest(self.headers.get('authorization', ''), 'Bearer ' + CONTROL_TOKEN)

    def do_POST(self):
        if self.path == '/control':
            if not self._authorized():
                self._json(403, {'error': 'forbidden'})
                return
            payload = json.loads(self.rfile.read(int(self.headers.get('content-length', '0'))))
            with CONTROL_LOCK:
                if CONTROL['started_at'] is not None:
                    self._json(409, {'error': 'already started'})
                    return
                duration = float(payload['duration_seconds'])
                if duration <= 0:
                    self._json(400, {'error': 'duration must be positive'})
                    return
                CONTROL.update(started_at=time.time() + 3, ends_at=time.time() + 3 + duration)
            self._json(200, CONTROL)
            return
        length = int(self.headers.get('content-length', '0'))
        payload = self.rfile.read(length).decode()
        if self.path == '/heartbeat':
            heartbeat = json.loads(payload)
            with HEARTBEAT_LOCK:
                LATEST_HEARTBEATS[heartbeat['worker_id']] = heartbeat
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
        if self.path == '/control':
            if not self._authorized():
                self._json(403, {'error': 'forbidden'})
                return
            with CONTROL_LOCK:
                self._json(200, dict(CONTROL))
        elif self.path == '/status':
            with HEARTBEAT_LOCK:
                heartbeats = list(LATEST_HEARTBEATS.values())
            self._json(200, {'heartbeats': heartbeats})
        elif self.path == '/events':
            events = EVENTS.read_text().splitlines() if EVENTS.exists() else []
            self._json(200, {'events': [json.loads(x) for x in events if x]})
        elif self.path == '/metrics':
            metrics = METRICS.read_text().splitlines() if METRICS.exists() else []
            self._json(200, {'metrics': [json.loads(x) for x in metrics if x]})
        else:
            self._json(404, {'error': 'not found'})


server = ThreadingHTTPServer(('0.0.0.0', __SOAK_CONTROLLER_PORT__), Handler)
server.daemon_threads = True
server.serve_forever()
PY
chmod 0755 /usr/local/bin/soak-controller
nohup /usr/local/bin/soak-controller >/var/log/soak-controller.log 2>&1 &
"""
    return script.replace("__SOAK_CONTROL_TOKEN__", repr(token)).replace(
        "__SOAK_CONTROLLER_PORT__", str(port)
    )

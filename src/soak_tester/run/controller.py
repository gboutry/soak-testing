from __future__ import annotations


def controller_cloud_init() -> str:
    return """#cloud-config
packages:
  - python3
write_files:
  - path: /usr/local/bin/soak-controller
    permissions: '0755'
    content: |
      #!/usr/bin/env python3
      import json
      from http.server import BaseHTTPRequestHandler, HTTPServer
      from pathlib import Path
      ROOT = Path('/var/lib/soak-tester')
      ROOT.mkdir(parents=True, exist_ok=True)
      HEARTBEATS = ROOT / 'heartbeats.jsonl'
      EVENTS = ROOT / 'events.jsonl'
      class Handler(BaseHTTPRequestHandler):
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
              target = HEARTBEATS if self.path == '/heartbeat' else EVENTS
              with target.open('a') as stream:
                  stream.write(payload + '\\n')
              self._json(200, {'ok': True})
          def do_GET(self):
              if self.path == '/status':
                  heartbeats = HEARTBEATS.read_text().splitlines()[-200:] if HEARTBEATS.exists() else []
                  self._json(200, {'heartbeats': [json.loads(x) for x in heartbeats if x]})
              elif self.path == '/events':
                  events = EVENTS.read_text().splitlines()[-200:] if EVENTS.exists() else []
                  self._json(200, {'events': [json.loads(x) for x in events if x]})
              else:
                  self._json(404, {'error': 'not found'})
      HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
runcmd:
  - nohup /usr/local/bin/soak-controller >/var/log/soak-controller.log 2>&1 &
"""

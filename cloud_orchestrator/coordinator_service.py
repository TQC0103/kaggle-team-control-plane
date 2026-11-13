"""Private, finite CPU-fixture control service. No scientific dispatch yet."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os

from .coordinator import FirestoreCoordinatorStore, cycle
from .kaggle_monitor import GoogleKaggleSessions
from .transport import KaggleTransport


def main():
    if os.environ.get('KCP_PROGRAM_MODE') != 'cpu_fixture':
        raise SystemExit('Only bounded CPU fixtures are enabled')
    store = FirestoreCoordinatorStore(os.environ['GOOGLE_CLOUD_PROJECT'])
    configurations = json.loads(os.environ['KCP_CLOUD_VAULTS'])
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, value):
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if self.path != '/health':return self.reply(404, {'error':'not_found'})
            return self.reply(200, dict(mode='cpu_fixture', scientific_submission_enabled=False,
                cpu_fixture_cap=2, source_sha256=os.environ.get('KCP_BUILD_SOURCE_SHA')))
        def do_POST(self):
            if self.path != '/tick':return self.reply(404, {'error':'not_found'})
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length','0') != '0':
                return self.reply(400, {'error':'empty_body_required'})
            try:
                value = cycle(store, KaggleTransport(GoogleKaggleSessions(configurations)))
                return self.reply(200, value)
            except Exception:
                return self.reply(503, {'error':'coordinator_unavailable'})
        def log_message(self, *_):pass
    ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()


if __name__ == '__main__':main()

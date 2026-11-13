"""Private finite DF65 tail service, activated only by exact owner handover."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os

from .coordinator import FirestoreCoordinatorStore
from .fm_tail import cycle
from .fm_transport import FMTailTransport
from .kaggle_monitor import GoogleKaggleSessions


def main():
    if os.environ.get('KCP_PROGRAM_MODE')!='df65_tail_v1':raise SystemExit('fixed DF65 tail mode required')
    store=FirestoreCoordinatorStore(os.environ['GOOGLE_CLOUD_PROJECT'],prefix='kcp_fm')
    vaults=json.loads(os.environ['KCP_CLOUD_VAULTS'])
    class Handler(BaseHTTPRequestHandler):
        def reply(self,status,value):
            data=json.dumps(value).encode();self.send_response(status)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_GET(self):
            if self.path!='/health':return self.reply(404,{'error':'not_found'})
            self.reply(200,dict(mode='df65_tail_v1',source_sha256=os.environ.get('KCP_BUILD_SOURCE_SHA'),scope='DF65_seed1337_14400_to_15000',gpu_cap=1,cpu_cap=2))
        def do_POST(self):
            if self.path!='/tick':return self.reply(404,{'error':'not_found'})
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length','0')!='0':return self.reply(400,{'error':'empty_body_required'})
            try:self.reply(200,cycle(store,FMTailTransport(GoogleKaggleSessions(vaults)),expected_plan_sha=os.environ['KCP_FM_PLAN_SHA']))
            except Exception:self.reply(503,{'error':'finite_coordinator_unavailable'})
        def log_message(self,*a):pass
    ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()


if __name__=='__main__':main()

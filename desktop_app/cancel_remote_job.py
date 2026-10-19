"""CLI for the updated KCP remote-cancellation API; never reads credentials.

python -m desktop_app.cancel_remote_job JOB_ID --session-id ID --session-ref OWNER/SLUG
Omit the optional ID for automatic owner/slug/version redirect resolution.
"""
import argparse
import json
from urllib.request import Request, urlopen

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('--session-id', type=int)
    parser.add_argument('--session-ref')
    args = parser.parse_args()
    if (args.session_id is None) != (args.session_ref is None):
        parser.error('--session-id and --session-ref must be supplied together')
    body = {} if args.session_id is None else {'kernel_session_id':args.session_id, 'session_ref':args.session_ref}
    req = Request('http://127.0.0.1:8765/api/jobs/'+args.job_id+'/cancel',
                  data=json.dumps(body).encode(), headers={'Content-Type':'application/json'}, method='POST')
    with urlopen(req, timeout=100) as response:
        data=json.load(response)
    print(json.dumps({'job_id':args.job_id, 'status':data['job']['status'],
                      'cancel_semantics':data['cancel_semantics'], 'message':data.get('message')}))

if __name__=='__main__': main()

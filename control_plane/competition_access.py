"""Read-only competition membership discovery through the KCP auth boundary."""
import csv
import io
from urllib.parse import urlparse
from .dataset_transfer import _account_cli

def entered_competitions(service, account_id):
    _, run = _account_cli(service, account_id, max_text_chars=64000)
    text = run(['competitions', 'list', '--group', 'entered', '--page-size', '100', '--csv'], 30)
    lines = text.splitlines()
    start = next((i for i,l in enumerate(lines) if l.startswith('ref,')), None)
    if start is None:
        if 'No competitions found' in text or text.strip() in ('Not found', ''):
            return dict(account_id=account_id, competitions=[], membership_only=True, advanced_access='unverified', truncated=False)
        raise ValueError('Unrecognized competition-list response; membership not established')
    rows=[]
    for row in csv.DictReader(io.StringIO('\n'.join(lines[start:]))):
        ref=row.get('ref','')
        if not ref:continue
        slug=urlparse(ref).path.rstrip('/').split('/')[-1]
        rows.append(dict(slug=slug,title=row.get('title',''),deadline=row.get('deadline',''),url='https://www.kaggle.com/competitions/'+slug))
    return dict(account_id=account_id, competitions=rows, membership_only=True, advanced_access='unverified',
        truncated=len(rows)>=100 or 'Next Page Token' in text or len(text)>=64000,
        note='Membership is not accelerator entitlement. No entry/rules acceptance/submission performed.')

"""One read-only Merkle-proof dependency repair for the fixed eight-GET sample."""
from datetime import datetime, timezone
import hashlib, json, ssl, urllib.request
from pathlib import Path

path = Path(__file__).with_name('block-source-sample.json')
sample = json.loads(path.read_text())
assert len(sample['receipts']) == 8 and sample['selected_height'] == sample['tip_at_request'] - 6
endpoint = '/tx/' + sample['coinbase_txid'] + '/merkle-proof'
assert all(r['path'] != endpoint for r in sample['receipts'])
start = datetime.now(timezone.utc).isoformat()
with urllib.request.urlopen(urllib.request.Request(sample['source'] + endpoint,
        headers={'Accept': 'application/json'}), timeout=12,
        context=ssl.create_default_context()) as response:
    body, status, date = response.read(), response.status, response.headers.get('Date')
end = datetime.now(timezone.utc).isoformat()
if status != 200:
    raise ValueError(f'HTTP {status}')
sample['receipts'].append({'path': endpoint, 'request_utc': start,
    'receipt_utc': end, 'http_status': status, 'response_date': date,
    'sha256': hashlib.sha256(body).hexdigest(), 'body': json.loads(body)})
sample['proof_request_addendum'] = ('One read-only dependency repair after the original eight requests: '
    'original cap omitted the inclusion-proof endpoint. Original eight-response record is '
    'preserved separately; no sample reselection.')
path.write_text(json.dumps(sample, indent=2) + '\n')

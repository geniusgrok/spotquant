"""One bounded, read-only BTC block-source qualification sample."""
from datetime import datetime, timezone
import hashlib, json, ssl, urllib.request
from pathlib import Path

BASE = 'https://blockstream.info/api'
OUT = Path(__file__).with_name('block-source-sample.json')
receipts = []

def get(path):
    begin = datetime.now(timezone.utc).isoformat()
    req = urllib.request.Request(BASE + path, headers={'Accept': 'application/json, text/plain'})
    with urllib.request.urlopen(req, timeout=12, context=ssl.create_default_context()) as response:
        body = response.read()
        status = response.status
        date = response.headers.get('Date')
    end = datetime.now(timezone.utc).isoformat()
    if status != 200:
        raise ValueError(f'HTTP {status} for {path}')
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        value = body.decode('ascii').strip()
    receipts.append({'path': path, 'request_utc': begin, 'receipt_utc': end,
                     'http_status': status, 'response_date': date,
                     'sha256': hashlib.sha256(body).hexdigest(), 'body': value})
    return value

tip = int(get('/blocks/tip/height'))
height = tip - 6
block_hash = get('/block-height/' + str(height))
block = get('/block/' + block_hash)
status = get('/block/' + block_hash + '/status')
header = get('/block/' + block_hash + '/header')
txid = get('/block/' + block_hash + '/txid/0')
tx = get('/tx/' + txid)
tx_hex = get('/tx/' + txid + '/hex')
# Merkle proof is fetched separately only if the eight-request qualification sample succeeded.
OUT.write_text(json.dumps({'source': BASE, 'tip_at_request': tip, 'selected_height': height,
                           'selected_block_hash': block_hash, 'coinbase_txid': txid,
                           'receipts': receipts}, indent=2) + '\n')
print(json.dumps({'tip': tip, 'height': height, 'hash': block_hash,
                  'txid': txid, 'tx_count': block['tx_count'],
                  'header_time': block['timestamp'], 'median_time_past': block['mediantime'],
                  'best_chain': status['in_best_chain'], 'outputs_sat': [v['value'] for v in tx['vout']],
                  'first_request_utc': receipts[0]['request_utc'],
                  'last_receipt_utc': receipts[-1]['receipt_utc'],
                  'request_count': len(receipts)}))

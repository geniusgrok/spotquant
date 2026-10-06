"""Verify locally recorded block header and coinbase txid/output values."""
import hashlib, json
from pathlib import Path

sample = json.loads(Path(__file__).with_name('block-source-sample.json').read_text())
responses = {x['path']: x['body'] for x in sample['receipts']}
height = sample['selected_height']
block_hash = sample['selected_block_hash']
txid = sample['coinbase_txid']
header = bytes.fromhex(responses[f'/block/{block_hash}/header'])
assert len(header) == 80
sha256d = lambda b: hashlib.sha256(hashlib.sha256(b).digest()).digest()
assert sha256d(header)[::-1].hex() == block_hash
block = responses[f'/block/{block_hash}']
assert block['height'] == height and block['id'] == block_hash
assert int.from_bytes(header[68:72], 'little') == block['timestamp']
assert header[36:68][::-1].hex() == block['merkle_root']
assert responses[f'/block/{block_hash}/status']['in_best_chain'] is True

def varint(b, i):
    marker = b[i]
    if marker < 0xfd: return marker, i + 1
    length = {0xfd: 2, 0xfe: 4, 0xff: 8}[marker]
    return int.from_bytes(b[i+1:i+1+length], 'little'), i+1+length

raw = bytes.fromhex(responses[f'/tx/{txid}/hex'])
version = raw[:4]
i = 4
segwit = raw[i:i+2] == b'\x00\x01'
if segwit: i += 2
inputs_start = i
vin_count, i = varint(raw, i)
assert vin_count == 1
for _ in range(vin_count):
    assert raw[i:i+32] == bytes(32)
    assert int.from_bytes(raw[i+32:i+36], 'little') == 0xffffffff
    i += 36
    script_len, i = varint(raw, i)
    i += script_len + 4
out_count, i = varint(raw, i)
values = []
for _ in range(out_count):
    values.append(int.from_bytes(raw[i:i+8], 'little'))
    i += 8
    script_len, i = varint(raw, i)
    i += script_len
outputs_end = i
if segwit:
    for _ in range(vin_count):
        item_count, i = varint(raw, i)
        for _ in range(item_count):
            item_len, i = varint(raw, i)
            i += item_len
locktime = raw[i:i+4]
assert len(locktime) == 4 and i+4 == len(raw)
no_witness = version + raw[inputs_start:outputs_end] + locktime
assert sha256d(no_witness)[::-1].hex() == txid
assert values == [x['value'] for x in responses[f'/tx/{txid}']['vout']]
proof = responses[f'/tx/{txid}/merkle-proof']
assert proof['block_height'] == height and proof['pos'] == 0
branch_hash = bytes.fromhex(txid)[::-1]
position = proof['pos']
for sibling_hex in proof['merkle']:
    sibling = bytes.fromhex(sibling_hex)[::-1]
    branch_hash = sha256d(sibling + branch_hash if position & 1 else branch_hash + sibling)
    position >>= 1
assert branch_hash[::-1].hex() == block['merkle_root']
assert responses[f'/tx/{txid}']['vin'][0]['is_coinbase'] is True
subsidy_sat = (50 * 100_000_000) >> (height // 210_000)
fee_claim_sat = sum(values) - subsidy_sat
assert fee_claim_sat >= 0
print(json.dumps({'header_hash_verified': True, 'header_time_verified': True,
                  'coinbase_txid_verified': True, 'output_values_sat_verified': True,
                  'merkle_inclusion_verified': True,
                  'height': height, 'subsidy_sat': subsidy_sat,
                  'coinbase_outputs_sat': values, 'fee_claim_above_subsidy_sat': fee_claim_sat,
                  'full_observation_receipt_utc': sample['receipts'][-1]['receipt_utc']}))

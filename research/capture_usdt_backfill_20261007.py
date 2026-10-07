"""One fixed-calendar public response; no account or session-dependent query."""
import hashlib,json,sys,time,urllib.error,urllib.request
from pathlib import Path
URL='https://api.coingecko.com/api/v3/coins/tether/market_chart?vs_currency=usd&days=365&interval=daily&precision=full'
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
if list(out.iterdir()): raise SystemExit('fixed-calendar directory must be empty')
started=time.time_ns()//1_000_000
try:r=urllib.request.urlopen(urllib.request.Request(URL,headers={'User-Agent':'spotquant-research/1.0'}),timeout=15)
except urllib.error.HTTPError as exc:r=exc
with r:
 status=r.status;final=r.url;server_date=r.headers.get('Date');content_type=r.headers.get('Content-Type');raw=r.read(250001)
received=time.time_ns()//1_000_000
if final!=URL or len(raw)>250000:raise SystemExit('unexpected redirect or response size')
sha=hashlib.sha256(raw).hexdigest()
name=f'{received}-{sha[:12]}'
with (out/(name+'.raw.json')).open('xb') as stream:stream.write(raw)
meta=dict(format='spotquant-usdt-cap-fixed-calendar-v1',url=URL,started_ms=started,received_ms=received,http_status=status,server_date=server_date,content_type=content_type,body_file=name+'.raw.json',body_sha256=sha,bytes=len(raw),data_era='CONDITIONAL_BACKFILL; first historical receipt unknown')
with (out/(name+'.receipt.json')).open('xb') as stream:stream.write((json.dumps(meta,sort_keys=True,separators=(',',':'))+'\n').encode())
print(json.dumps({k:meta[k] for k in ('http_status','received_ms','body_sha256','bytes','body_file')},sort_keys=True))

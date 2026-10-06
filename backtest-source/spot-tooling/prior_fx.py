import bisect,json
from decimal import Decimal as D
from pathlib import Path

class PriorFX:
    def __init__(self, path):
        rates = json.loads(Path(path).read_text())['rates']
        self.days = sorted(rates)
        self.rates = [D(str(rates[k]['CNY'])) for k in self.days]

    def __call__(self, stamp):
        from datetime import datetime, timezone
        day = datetime.fromtimestamp(stamp / 1000, timezone.utc).date().isoformat()
        i = bisect.bisect_left(self.days, day) - 1
        return self.rates[i] if i >= 0 else D('6.9615')

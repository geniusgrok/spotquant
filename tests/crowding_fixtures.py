"""Explicit synthetic causal inputs, never production market evidence."""
from contextlib import contextmanager
from decimal import Decimal as D
from pathlib import Path
import tempfile
from unittest.mock import patch

from spotquant.crowding import DAY, FUNDING_LAG, BASIS_LAG
from spotquant.model import ORIGIN


class KnownFeatures:
    def value(self, name, now):
        observed = now - FUNDING_LAG if name == 'funding' else now // DAY * DAY
        self.last_lookup = dict(name=name, observation_ms=observed,
                                available_ms=observed + (FUNDING_LAG if name == 'funding' else BASIS_LAG),
                                value='.0001', cause=None, synthetic=True)
        return D('.0001')


@contextmanager
def historical_features():
    from research.edge_features import FeatureBook, _canonical
    from research import edge_spot
    from test_edge_features import artifact, row
    start, end = ORIGIN + 390 * DAY, ORIGIN + 420 * DAY
    body = artifact([row('funding', t, '.0001') for t in range(start, end, FUNDING_LAG)],
                    [row('basis', t, '.001') for t in range(start, end, DAY)])
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'synthetic-features.json'; path.write_bytes(_canonical(body))
        features = FeatureBook(path)
        # Only the fixture checksum is substituted; source causality/schema still run.
        with patch.object(edge_spot, 'FEATURE_SHA256', features.sha256):
            yield features


def measure(*args, **kwargs):
    from research.adoption_spot import measure as canonical_measure
    with historical_features() as features:
        return canonical_measure(*args, features=features, **kwargs)

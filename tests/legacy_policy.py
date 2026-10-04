"""Scope historical crowding fixtures explicitly; main uses the BTC target core."""
from contextlib import ExitStack
from unittest.mock import patch
from spotquant import crowding, preview


def legacy_policy():
    stack = None
    def start():
        nonlocal stack
        stack = ExitStack()
        stack.enter_context(patch('spotquant.session.portfolio', preview.decision))
        stack.enter_context(patch('spotquant.session.RULE', crowding.RULE))
    def stop():
        stack.close()
    return start, stop

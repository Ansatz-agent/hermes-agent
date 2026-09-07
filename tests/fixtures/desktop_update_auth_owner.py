"""Isolated native auth owner for Desktop updater integration tests."""

import sys

from hermes_cli.client_auth.runtime import OwnerBroker, RuntimeConsumer, RuntimeSnapshot


class FixtureOwner:
    def __init__(self):
        self._snapshot = RuntimeSnapshot.new_authenticated(
            "update-test", now=0.0, ttl=10**12,
        )
        if sys.argv[1:] == ["signed-out"]:
            self._snapshot = self._snapshot.locked("signed_out", now=0.0)

    def refresh(self):
        return self._snapshot

    def snapshot(self):
        return self._snapshot

    def connect_consumer(self, **_kwargs):
        return RuntimeConsumer(self._snapshot, liveness_probe=lambda: True, clock=lambda: 0.0)


broker = OwnerBroker.start(FixtureOwner())
try:
    print("ready", flush=True)
    sys.stdin.read()
finally:
    broker.close()

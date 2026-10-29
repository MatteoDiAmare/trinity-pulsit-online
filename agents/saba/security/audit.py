"""Structured audit event sink with no secret values."""
import json
from datetime import datetime, timezone


class Audit:
    def __init__(self, sink):
        self.sink = sink

    def record(self, event, **metadata):
        self.sink.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, "metadata": metadata}) + "\n")
        self.sink.flush()

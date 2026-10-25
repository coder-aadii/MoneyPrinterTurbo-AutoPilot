"""Standalone browser-independent worker for scheduled Autopilot production."""

from __future__ import annotations

import signal
import threading

from app.services.autopilot import AutopilotService


def main() -> int:
    """Keep the durable scheduler and generation workers alive until stopped."""
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    service = AutopilotService()
    try:
        while not stop.wait(1):
            pass
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Docker HEALTHCHECK: confirms the service answers its own API.

Reads the configured web port from config.json when present, so it stays
correct if the port is changed in the UI, rather than assuming the default.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

DEFAULT_PORT = 8080


def _port() -> int:
    cfg_dir = os.environ.get("AMBIWLED_CONFIG_DIR", "/config")
    try:
        with open(os.path.join(cfg_dir, "config.json")) as fh:
            return int(json.load(fh).get("web", {}).get("port", DEFAULT_PORT))
    except Exception:
        return DEFAULT_PORT


def main() -> int:
    port = _port()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=3) as r:
            return 0 if r.status < 500 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())

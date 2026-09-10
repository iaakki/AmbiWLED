#!/usr/bin/env python3
"""Write the ceiling ledmap to a file, using the service's own generator.

Normally you would press "Continuous sweep" in the Output panel, which builds
and uploads this for you.  This exists for inspecting the map by hand.

    ./tools/make_ledmap.py --from-ambiwled 198.51.100.5:8080 -o ledmap1.json
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ambiwled.wled import ledmap_from_edges          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-ambiwled", metavar="HOST:PORT", default="127.0.0.1:8080")
    ap.add_argument("-o", "--out", default="ledmap1.json")
    args = ap.parse_args()

    cfg = json.loads(urllib.request.urlopen(
        f"http://{args.from_ambiwled}/api/config", timeout=6).read())
    mapping = ledmap_from_edges(cfg)
    Path(args.out).write_text(json.dumps({"n": "AmbiWled front-to-rear", "map": mapping}))

    n = len(mapping)
    print(f"wrote {args.out}: {n} entries")
    print(f"  logical 0     -> physical {mapping[0]}  (front centre)")
    print(f"  logical {n//2:<5} -> physical {mapping[n//2]}  (rear centre)")
    print(f"  logical {n-1:<5} -> physical {mapping[-1]}  (front centre, other side)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

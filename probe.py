#!/usr/bin/env python3
"""Standalone Ambilight probe — stdlib only, no install needed.

    ./probe.py 192.0.2.10            # dump one frame + detected layout
    ./probe.py 192.0.2.10 --rate     # measure the achievable poll rate
    ./probe.py 192.0.2.10 --watch    # live zone colours in the terminal
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request


def get(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def detect_api(ip: str) -> int:
    for v in (6, 1):
        try:
            data = get(f"http://{ip}:1925/{v}/system", timeout=3.0)
            major = int(data.get("api_version", {}).get("Major", v))
            print(f"TV      : {data.get('name', '?')} ({data.get('model_encrypted') and 'encrypted model'})")
            print(f"API     : reported major {major} -> using /{1 if major == 1 else 6}/")
            return 1 if major == 1 else 6
        except Exception:
            continue
    print("API     : could not read /system, assuming 6")
    return 6


def layout_of(payload: dict) -> list[tuple[str, int]]:
    layer = payload.get("layer1")
    if not isinstance(layer, dict):
        layer = next((v for v in payload.values() if isinstance(v, dict) and v), {})
    return [(side, len(zones)) for side, zones in layer.items() if isinstance(zones, dict) and zones]


def zones_of(payload: dict) -> dict[str, list[tuple[int, int, int]]]:
    layer = payload.get("layer1")
    if not isinstance(layer, dict):
        layer = next((v for v in payload.values() if isinstance(v, dict) and v), {})
    out = {}
    for side, zones in layer.items():
        if not isinstance(zones, dict):
            continue
        keys = sorted(zones, key=lambda k: int(k) if str(k).lstrip("-").isdigit() else 0)
        out[side] = [(zones[k].get("r", 0), zones[k].get("g", 0), zones[k].get("b", 0)) for k in keys]
    return out


def swatch(rgb) -> str:
    r, g, b = (int(c) for c in rgb)
    return f"\x1b[48;2;{r};{g};{b}m  \x1b[0m"


def main() -> int:
    ap = argparse.ArgumentParser(description="Philips Ambilight probe")
    ap.add_argument("ip")
    ap.add_argument("--endpoint", default="processed", choices=["processed", "measured"])
    ap.add_argument("--rate", action="store_true", help="measure achievable poll rate for 5 s")
    ap.add_argument("--watch", action="store_true", help="live colour bars until Ctrl-C")
    ap.add_argument("--raw", action="store_true", help="print the raw JSON and exit")
    args = ap.parse_args()

    api = detect_api(args.ip)
    url = f"http://{args.ip}:1925/{api}/ambilight/{args.endpoint}"
    print(f"Endpoint: {url}\n")

    try:
        payload = get(url)
    except urllib.error.URLError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    if args.raw:
        print(json.dumps(payload, indent=2))
        return 0

    sides = layout_of(payload)
    total = sum(c for _, c in sides)
    print("Detected layout")
    for name, count in sides:
        print(f"  {name:<8} {count:>3} zones")
    print(f"  {'TOTAL':<8} {total:>3} zones")
    if not any(n == "bottom" for n, _ in sides):
        print("  note: no 'bottom' layer — the rear ceiling edge must be synthesised")
    print()

    print("Current colours (index order)")
    for name, colours in zones_of(payload).items():
        print(f"  {name:<8} " + "".join(swatch(c) for c in colours))
    print()

    if args.rate:
        print("Measuring poll rate for 5 s ...")
        times, started, n = [], time.monotonic(), 0
        while time.monotonic() - started < 5.0:
            t0 = time.monotonic()
            try:
                get(url, timeout=2.0)
                times.append(time.monotonic() - t0)
                n += 1
            except Exception:
                pass
        span = time.monotonic() - started
        if times:
            print(f"  requests   : {n} in {span:.1f} s -> {n / span:.1f} Hz")
            print(f"  latency    : median {statistics.median(times)*1000:.0f} ms, "
                  f"min {min(times)*1000:.0f} ms, max {max(times)*1000:.0f} ms")
        else:
            print("  no successful requests")

    if args.watch:
        print("Watching (Ctrl-C to stop)\n")
        try:
            while True:
                z = zones_of(get(url))
                line = " | ".join("".join(swatch(c) for c in v) for v in z.values())
                print("\r" + line, end="", flush=True)
                time.sleep(0.1)
        except KeyboardInterrupt:
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

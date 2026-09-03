#!/usr/bin/env python3
"""Confirm the Ambilight zone index direction (spec §9.2).

Identify tells you which *physical* LED run is which. This tells you something
different and still unverified: whether the TV's zone indices run the way the
mapping assumes — left bottom→top, top left→right, right top→bottom.

    ./tools/corner_test.py --make            # write corner-test.png
    ./tools/corner_test.py --check <tv-ip>   # read the zones and judge

Display the PNG full-screen on the TV (USB stick, DLNA, cast, or a browser on
the TV), with Ambilight on, then run --check.  Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import urllib.request
import zlib

W, H = 1920, 1080
BAND = 150                      # border thickness Ambilight samples from

RED, GREEN, BLUE, YELLOW = (220, 30, 30), (30, 200, 60), (40, 70, 230), (230, 200, 40)
CORNERS = {"top-left": RED, "top-right": GREEN, "bottom-right": BLUE, "bottom-left": YELLOW}
NAMES = {RED: "RED", GREEN: "GREEN", BLUE: "BLUE", YELLOW: "YELLOW"}


def build_rows() -> list[bytearray]:
    rows = []
    for y in range(H):
        row = bytearray(W * 3)
        in_band_y = y < BAND or y >= H - BAND
        for x in range(W):
            if not (in_band_y or x < BAND or x >= W - BAND):
                continue                       # interior stays black
            colour = (RED if x < W // 2 else GREEN) if y < H // 2 else \
                     (YELLOW if x < W // 2 else BLUE)
            row[x * 3: x * 3 + 3] = bytes(colour)
        rows.append(row)
    return rows


def write_png(path: str) -> None:
    raw = b"".join(b"\x00" + bytes(r) for r in build_rows())

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)
    print(f"wrote {path}  ({W}x{H}, {len(png)/1024:.0f} kB)")
    print("corners: top-left RED, top-right GREEN, bottom-right BLUE, bottom-left YELLOW")


# -- checking ----------------------------------------------------------------

def classify(rgb) -> str:
    r, g, b = rgb
    if max(r, g, b) < 25:
        return "dark"
    if r > 90 and g > 90 and b < max(r, g) * 0.6:
        return "YELLOW"
    if r >= g and r >= b:
        return "RED"
    if g >= r and g >= b:
        return "GREEN"
    return "BLUE"


def zones_of(ip: str, api: int, endpoint: str):
    url = f"http://{ip}:1925/{api}/ambilight/{endpoint}"
    payload = json.loads(urllib.request.urlopen(url, timeout=4).read())
    layer = payload.get("layer1") or next(iter(payload.values()))
    out = {}
    for side, zones in layer.items():
        keys = sorted(zones, key=lambda k: int(k))
        out[side] = [(zones[k]["r"], zones[k]["g"], zones[k]["b"]) for k in keys]
    return out


def swatch(rgb):
    return f"\x1b[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m  \x1b[0m"


def check(ip: str, api: int, endpoint: str) -> int:
    z = zones_of(ip, api, endpoint)
    for side in ("left", "top", "right"):
        if side not in z:
            print(f"side '{side}' missing from the TV payload", file=sys.stderr)
            return 2

    print("Zones as reported by the TV\n")
    for side in ("left", "top", "right"):
        bar = "".join(swatch(c) for c in z[side])
        print(f"  {side:<6} {bar}  {[classify(c) for c in z[side]]}")

    # Each side spans two corners; the ends tell us which way the index runs.
    findings, ok = [], True

    top_first, top_last = classify(z["top"][0]), classify(z["top"][-1])
    if (top_first, top_last) == ("RED", "GREEN"):
        findings.append(("top", "left -> right", True))
    elif (top_first, top_last) == ("GREEN", "RED"):
        findings.append(("top", "right -> left  (REVERSED vs assumption)", False)); ok = False
    else:
        findings.append(("top", f"inconclusive ({top_first} -> {top_last})", False)); ok = False

    l_first, l_last = classify(z["left"][0]), classify(z["left"][-1])
    if (l_first, l_last) == ("YELLOW", "RED"):
        findings.append(("left", "bottom -> top", True))
    elif (l_first, l_last) == ("RED", "YELLOW"):
        findings.append(("left", "top -> bottom  (REVERSED vs assumption)", False)); ok = False
    else:
        findings.append(("left", f"inconclusive ({l_first} -> {l_last})", False)); ok = False

    r_first, r_last = classify(z["right"][0]), classify(z["right"][-1])
    if (r_first, r_last) == ("GREEN", "BLUE"):
        findings.append(("right", "top -> bottom", True))
    elif (r_first, r_last) == ("BLUE", "GREEN"):
        findings.append(("right", "bottom -> top  (REVERSED vs assumption)", False)); ok = False
    else:
        findings.append(("right", f"inconclusive ({r_first} -> {r_last})", False)); ok = False

    print("\nIndex direction")
    for side, verdict, good in findings:
        print(f"  {'OK  ' if good else 'FAIL'}  {side:<6} runs {verdict}")

    if ok:
        print("\nThe clockwise loop assumed by the default mapping is CONFIRMED.")
        print("No config change needed.")
    else:
        print("\nThe assumption does not hold. For each side marked REVERSED,")
        print("tick 'Src rev' on the edge that consumes it in the Mapping panel.")
        print("If a side is inconclusive, check the image really is full-screen")
        print("with no letterboxing, and that Ambilight is on.")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--make", action="store_true", help="write corner-test.png")
    ap.add_argument("--check", metavar="TV_IP", help="read the TV and judge the direction")
    ap.add_argument("--out", default="corner-test.png")
    ap.add_argument("--api", type=int, default=6)
    ap.add_argument("--endpoint", default="processed")
    args = ap.parse_args()

    if args.make:
        write_png(args.out)
    if args.check:
        return check(args.check, args.api, args.endpoint)
    if not args.make:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

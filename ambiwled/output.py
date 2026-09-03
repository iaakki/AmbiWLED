"""WLED UDP realtime output (DNRGB, protocol 4)."""
from __future__ import annotations

import logging
import socket
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

DNRGB = 4
DNRGB_PORT = 21324
# WLED's per-packet ceiling for DNRGB.
MAX_LEDS_PER_PACKET = 489


class Sender:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.packets_sent = 0
        self.send_errors = 0
        self.update(cfg)

    def update(self, cfg: dict[str, Any]) -> None:
        o = cfg.get("output", {})
        self.timeout_byte = max(1, min(int(o.get("timeout_s", 2)), 254))
        self.targets = [t for t in o.get("targets", []) if t.get("enabled", True)]

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, frame: np.ndarray) -> None:
        """frame: (n, 3) float 0..255.  Fire and forget, no retries."""
        data = np.clip(frame + 0.5, 0, 255).astype(np.uint8)
        for t in self.targets:
            host = t.get("host")
            if not host:
                continue
            port = int(t.get("port", DNRGB_PORT))
            offset = int(t.get("pixel_offset", 0))
            count = int(t.get("pixel_count", len(data)))
            chunk = data[offset : offset + count]
            self._send_pixels(host, port, offset, chunk)

    def _send_pixels(self, host: str, port: int, start: int, pixels: np.ndarray) -> None:
        # 462 fits in one packet, but keep the multi-packet path so a larger
        # layout is never silently truncated.
        for i in range(0, len(pixels), MAX_LEDS_PER_PACKET):
            block = pixels[i : i + MAX_LEDS_PER_PACKET]
            index = start + i
            header = bytes([DNRGB, self.timeout_byte, (index >> 8) & 0xFF, index & 0xFF])
            try:
                self.sock.sendto(header + block.tobytes(), (host, port))
                self.packets_sent += 1
            except OSError as exc:
                self.send_errors += 1
                if self.send_errors % 100 == 1:
                    log.warning("UDP send to %s:%s failed: %s", host, port, exc)

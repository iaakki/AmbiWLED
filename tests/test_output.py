"""DNRGB wire format.  Getting a byte wrong here is invisible until the LEDs are."""
from __future__ import annotations

import numpy as np
import pytest

from ambiwled.output import MAX_LEDS_PER_PACKET, Sender


def sender_for(host, port, count=462, timeout_s=2, offset=0):
    return Sender({"output": {"timeout_s": timeout_s, "targets": [
        {"host": host, "port": port, "pixel_offset": offset,
         "pixel_count": count, "enabled": True}]}})


def decode(pkt):
    return {"protocol": pkt[0], "timeout": pkt[1],
            "start": (pkt[2] << 8) | pkt[3],
            "pixels": [tuple(pkt[4 + i * 3: 7 + i * 3]) for i in range((len(pkt) - 4) // 3)]}


def test_single_packet_for_462_pixels(udp_sink):
    host, port, recv = udp_sink
    s = sender_for(host, port)
    frame = np.zeros((462, 3), dtype=np.float32)
    frame[0] = [255, 0, 0]
    frame[461] = [0, 0, 255]
    s.send(frame)

    packets = recv()
    assert len(packets) == 1, "462 pixels must fit one packet, no fragmentation"
    assert len(packets[0]) == 1390        # 4 header + 462*3
    d = decode(packets[0])
    assert d["protocol"] == 4             # DNRGB
    assert d["timeout"] == 2              # not 255: WLED must be able to fall back
    assert d["start"] == 0
    assert len(d["pixels"]) == 462
    assert d["pixels"][0] == (255, 0, 0)
    assert d["pixels"][461] == (0, 0, 255)
    s.close()


def test_large_layouts_are_split_not_truncated(udp_sink):
    """The pixel count must never be silently capped if the layout grows."""
    host, port, recv = udp_sink
    n = 1000
    s = sender_for(host, port, count=n)
    frame = np.tile(np.arange(n, dtype=np.float32)[:, None] % 256, (1, 3))
    s.send(frame)

    packets = sorted(recv(), key=lambda p: (p[2] << 8) | p[3])
    assert len(packets) == 3              # 489 + 489 + 22
    assert sum((len(p) - 4) // 3 for p in packets) == n
    starts = [decode(p)["start"] for p in packets]
    assert starts == [0, MAX_LEDS_PER_PACKET, MAX_LEDS_PER_PACKET * 2]
    # Reassemble and compare to the source frame.
    rebuilt = [px for p in packets for px in decode(p)["pixels"]]
    assert rebuilt[0] == (0, 0, 0)
    assert rebuilt[500] == (244, 244, 244)
    s.close()


def test_pixel_offset_addresses_the_right_leds(udp_sink):
    host, port, recv = udp_sink
    s = sender_for(host, port, count=100, offset=231)   # e.g. WLED bus 2
    frame = np.zeros((462, 3), dtype=np.float32)
    frame[231] = [1, 2, 3]
    s.send(frame)

    d = decode(recv()[0])
    assert d["start"] == 231
    assert len(d["pixels"]) == 100
    assert d["pixels"][0] == (1, 2, 3)
    s.close()


def test_float_values_are_rounded_not_truncated(udp_sink):
    host, port, recv = udp_sink
    s = sender_for(host, port, count=3)
    s.send(np.array([[0.4, 0.6, 254.5], [255.9, -3.0, 127.5], [1, 1, 1]], dtype=np.float32))
    d = decode(recv()[0])
    assert d["pixels"][0] == (0, 1, 255)
    assert d["pixels"][1] == (255, 0, 128)      # clamped both ends
    s.close()


@pytest.mark.parametrize("configured,expected", [(2, 2), (255, 254), (0, 1), (900, 254)])
def test_timeout_byte_is_clamped_to_a_usable_range(udp_sink, configured, expected):
    host, port, recv = udp_sink
    s = sender_for(host, port, count=1, timeout_s=configured)
    s.send(np.zeros((1, 3), dtype=np.float32))
    assert decode(recv()[0])["timeout"] == expected
    s.close()


def test_disabled_targets_are_not_sent(udp_sink):
    host, port, recv = udp_sink
    s = Sender({"output": {"timeout_s": 2, "targets": [
        {"host": host, "port": port, "pixel_offset": 0, "pixel_count": 4, "enabled": False}]}})
    s.send(np.zeros((4, 3), dtype=np.float32))
    assert recv() == []
    s.close()


def test_multiple_targets_each_receive_a_frame(udp_sink):
    host, port, recv = udp_sink
    s = Sender({"output": {"timeout_s": 2, "targets": [
        {"host": host, "port": port, "pixel_offset": 0, "pixel_count": 2, "enabled": True},
        {"host": host, "port": port, "pixel_offset": 2, "pixel_count": 2, "enabled": True}]}})
    s.send(np.zeros((4, 3), dtype=np.float32))
    assert len(recv()) == 2
    s.close()


def test_unreachable_target_does_not_raise(udp_sink):
    """Fire and forget: a dead controller must never stall the output loop."""
    s = sender_for("192.0.2.1", 21324, count=4)      # TEST-NET-1, unroutable
    s.send(np.zeros((4, 3), dtype=np.float32))       # must not raise
    s.close()

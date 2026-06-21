#!/usr/bin/env python3
"""Decode SunSDR2 DX device->PC TX telemetry packets (0x1F00 / 0x1F01).

Goal: identify which fields carry forward power / reflected power / SWR by
correlating float field values with the PTT on/off windows.

Usage: python3 analyze_telemetry.py /path/to/sunsdr_sdr_tx.pcap
"""
import sys, struct
import numpy as np


def packets(path):
    with open(path, 'rb') as f:
        data = f.read()
    magic = struct.unpack('<I', data[:4])[0]
    le = magic in (0xa1b2c3d4, 0xa1b23c4d)
    endian = '<' if le else '>'
    nano = magic in (0xa1b23c4d, 0x4d3cb2a1)
    off = 24
    n = len(data)
    while off + 16 <= n:
        ts_s, ts_u, incl, orig = struct.unpack(endian + 'IIII', data[off:off+16])
        off += 16
        pkt = data[off:off+incl]
        off += incl
        yield ts_s + ts_u / (1e9 if nano else 1e6), pkt


def parse_udp(pkt):
    if len(pkt) < 14:
        return None
    if pkt[12:14] != b'\x08\x00':
        return None
    ihl = (pkt[14] & 0x0f) * 4
    if pkt[23] != 17:
        return None
    ipoff = 14
    src = '.'.join(str(b) for b in pkt[ipoff+12:ipoff+16])
    dst = '.'.join(str(b) for b in pkt[ipoff+16:ipoff+20])
    udp = ipoff + ihl
    sport, dport, ulen, _ = struct.unpack('>HHHH', pkt[udp:udp+8])
    payload = pkt[udp+8:]
    return src, dst, sport, dport, payload


def decode_all_fields(payload):
    """Return dict of {offset: (u32, f32)} for every 4-byte window (step 2)."""
    out = {}
    for off in range(4, len(payload) - 3, 2):
        chunk = payload[off:off+4]
        u = struct.unpack('<I', chunk)[0]
        f = struct.unpack('<f', chunk)[0]
        out[off] = (u, f)
    return out


def main():
    path = sys.argv[1]
    DEV = '192.168.16.200'

    tel00 = []   # (ts, payload)
    tel01 = []
    ptt_events = []  # (ts, trailing)

    for ts, pkt in packets(path):
        u = parse_udp(pkt)
        if not u:
            continue
        src, dst, sport, dport, payload = u
        # device -> PC telemetry
        if src == DEV and len(payload) >= 4 and payload[0] == 0x32 and payload[1] == 0xff:
            sub = struct.unpack('<H', payload[2:4])[0]
            if sub == 0x1F00:
                tel00.append((ts, payload))
            elif sub == 0x1F01:
                tel01.append((ts, payload))
        # PC -> device PTT control (0x0006 on 50001)
        if dst == DEV and dport == 50001 and len(payload) >= 4:
            cmd = struct.unpack('<H', payload[2:4])[0]
            if cmd == 0x0006:
                trailing = struct.unpack('<I', payload[-4:])[0]
                ptt_events.append((ts, trailing))

    if not tel00:
        print("No 0x1F00 packets found")
        return

    t0 = tel00[0][0]
    print(f"0x1F00 count={len(tel00)}  0x1F01 count={len(tel01)}")
    print(f"PTT events: {[(round(t-t0,3), tr) for t, tr in ptt_events]}")
    print(f"0x1F00 payload len={len(tel00[0][1])}")

    # Build PTT windows (TX on intervals)
    tx_windows = []
    on = None
    for t, tr in ptt_events:
        if tr == 1:
            on = t
        elif tr == 0 and on is not None:
            tx_windows.append((on, t))
            on = None

    def in_tx(ts):
        return any(a <= ts <= b for a, b in tx_windows)

    # For each byte offset, compute float stats split by TX-on vs TX-off
    fields = sorted(decode_all_fields(tel00[0][1]).keys())
    print("\n=== 0x1F00 per-offset float: TX-on vs TX-off ===")
    print(f"{'off':>4} {'on_min':>10} {'on_max':>10} {'on_mean':>10} "
          f"{'off_mean':>10} {'varies?':>8}")
    for off in fields:
        on_vals, off_vals = [], []
        for ts, pl in tel00:
            if off + 4 > len(pl):
                continue
            f = struct.unpack('<f', pl[off:off+4])[0]
            if not np.isfinite(f) or abs(f) > 1e12:
                continue
            (on_vals if in_tx(ts) else off_vals).append(f)
        if not on_vals:
            continue
        on_arr = np.array(on_vals)
        off_arr = np.array(off_vals) if off_vals else np.array([0.0])
        varies = "YES" if (on_arr.max() - on_arr.min()) > 1e-3 else ""
        # Highlight fields that differ between TX-on and TX-off
        diff = abs(on_arr.mean() - off_arr.mean())
        flag = " <== TX-correlated" if diff > 0.1 else ""
        print(f"{off:>4} {on_arr.min():>10.4f} {on_arr.max():>10.4f} "
              f"{on_arr.mean():>10.4f} {off_arr.mean():>10.4f} {varies:>8}{flag}")

    # Dump a few raw TX-on samples for manual inspection
    print("\n=== sample 0x1F00 during TX (hex) ===")
    shown = 0
    for ts, pl in tel00:
        if in_tx(ts) and shown < 5:
            print(f"  t={ts-t0:6.3f}  {pl.hex()}")
            shown += 1

    if tel01:
        print(f"\n=== 0x1F01 sample (len={len(tel01[0][1])}) ===")
        for ts, pl in tel01[:3]:
            print(f"  t={ts-t0:6.3f}  {pl.hex()}")


if __name__ == "__main__":
    main()

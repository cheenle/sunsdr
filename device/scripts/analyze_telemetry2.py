#!/usr/bin/env python3
"""Decode SunSDR2 DX 0x1F00 TX telemetry: identify fwd power / SWR / temp.

Treats candidate fields as int16/uint16/float and correlates each against
the concurrent TX IQ envelope (0xFFFD packets) to find forward power.
"""
import sys, struct
import numpy as np

def packets(path):
    with open(path, 'rb') as f:
        data = f.read()
    magic = struct.unpack('<I', data[:4])[0]
    nano = magic in (0xa1b23c4d, 0x4d3cb2a1)
    off = 24
    n = len(data)
    while off + 16 <= n:
        ts_s, ts_u, incl, orig = struct.unpack('<IIII', data[off:off+16])
        off += 16
        pkt = data[off:off+incl]; off += incl
        yield ts_s + ts_u/(1e9 if nano else 1e6), pkt

def parse_udp(pkt):
    if len(pkt) < 14 or pkt[12:14] != b'\x08\x00':
        return None
    ihl = (pkt[14] & 0x0f) * 4
    if pkt[23] != 17:
        return None
    src = '.'.join(str(b) for b in pkt[26:30])
    dst = '.'.join(str(b) for b in pkt[30:34])
    udp = 14 + ihl
    sport, dport = struct.unpack('>HH', pkt[udp:udp+4])
    return src, dst, sport, dport, pkt[udp+8:]

DEV = '192.168.16.200'
path = sys.argv[1]

t0 = None
tel = []      # (t, payload) for 0x1F00
txiq = []     # (t, rms) for 0xFFFD
ptt = []
for ts, pkt in packets(path):
    u = parse_udp(pkt)
    if not u:
        continue
    src, dst, sp, dp, pl = u
    if t0 is None:
        t0 = ts
    t = ts - t0
    if len(pl) < 4 or pl[0] != 0x32 or pl[1] != 0xff:
        continue
    sub = struct.unpack('<H', pl[2:4])[0]
    if src == DEV and sub == 0x1F00:
        tel.append((t, pl))
    elif dst == DEV and sub == 0xFFFD and len(pl) >= 1210:
        body = pl[10:1210]
        acc = 0.0; cnt = 0
        for i in range(0, 1200, 6):
            iv = int.from_bytes(body[i:i+3], 'little', signed=True)
            qv = int.from_bytes(body[i+3:i+6], 'little', signed=True)
            acc += (iv/8388608.0)**2 + (qv/8388608.0)**2; cnt += 2
        txiq.append((t, (acc/cnt) ** 0.5))
    elif dst == DEV and sub == 0x0006:
        trail = struct.unpack('<I', pl[-4:])[0]
        ptt.append((t, trail))

print(f"0x1F00={len(tel)}  0xFFFD={len(txiq)}  PTT={ptt}")

# TX-on windows from PTT pairs
windows = []
on = None
for t, tr in ptt:
    if tr == 1:
        on = t
    elif tr == 0 and on is not None:
        windows.append((on, t)); on = None

def in_tx(t):
    return any(a <= t <= b for a, b in windows)

# Candidate field extractors on the payload
def u16(pl, o):  return struct.unpack('<H', pl[o:o+2])[0] if o+2 <= len(pl) else 0
def s16(pl, o):  return struct.unpack('<h', pl[o:o+2])[0] if o+2 <= len(pl) else 0
def f32(pl, o):  return struct.unpack('<f', pl[o:o+4])[0] if o+4 <= len(pl) else 0.0

# Build per-packet TX envelope by nearest 0xFFFD within 20ms
txiq_arr = np.array(txiq) if txiq else np.zeros((0, 2))
def env_at(t):
    if len(txiq_arr) == 0:
        return 0.0
    idx = np.argmin(np.abs(txiq_arr[:, 0] - t))
    if abs(txiq_arr[idx, 0] - t) > 0.05:
        return 0.0
    return txiq_arr[idx, 1]

print("\n=== candidate u16 fields: correlation with TX IQ envelope ===")
print(f"{'off':>4} {'on_min':>8} {'on_max':>8} {'on_mean':>9} {'off_mean':>9} {'corr_env':>9}")
for o in range(12, 18, 2):
    on_vals, off_vals, env_vals = [], [], []
    for t, pl in tel:
        v = u16(pl, o)
        if in_tx(t):
            on_vals.append(v); env_vals.append(env_at(t))
        else:
            off_vals.append(v)
    on_vals = np.array(on_vals, float)
    env_vals = np.array(env_vals, float)
    corr = 0.0
    if len(on_vals) > 5 and on_vals.std() > 0 and env_vals.std() > 0:
        corr = float(np.corrcoef(on_vals, env_vals)[0, 1])
    print(f"{o:>4} {on_vals.min():>8.1f} {on_vals.max():>8.1f} "
          f"{on_vals.mean():>9.2f} {np.mean(off_vals):>9.2f} {corr:>9.3f}")

print("\n=== candidate f32 fields ===")
for o in (18, 22, 26, 30):
    on_vals = [f32(pl, o) for t, pl in tel if in_tx(t)]
    off_vals = [f32(pl, o) for t, pl in tel if not in_tx(t)]
    if on_vals:
        print(f"  off={o}: on[min={min(on_vals):.3f} max={max(on_vals):.3f} "
              f"mean={np.mean(on_vals):.3f}] off_mean={np.mean(off_vals):.3f}")

# Time-series of the strongest field vs envelope for the biggest TX window
if windows:
    a, b = max(windows, key=lambda w: w[1]-w[0])
    print(f"\n=== time-series in window {a:.2f}-{b:.2f}s (field@14 vs IQ env) ===")
    rows = [(t, u16(pl, 14)) for t, pl in tel if a <= t <= b]
    for i in range(0, len(rows), max(1, len(rows)//20)):
        t, v = rows[i]
        print(f"  t={t:.3f}  field14={v:>4}  env={env_at(t):.4f}")

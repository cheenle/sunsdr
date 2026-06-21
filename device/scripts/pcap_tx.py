#!/usr/bin/env python3
"""Minimal pcap (classic + pcapng) UDP parser focused on SunSDR TX chain."""
import struct, sys, collections

def read_pcap(path):
    """Yield (ts, src_ip, sport, dst_ip, dport, udp_payload) for UDP packets."""
    with open(path, 'rb') as f:
        data = f.read()
    magic = data[:4]
    if magic in (b'\xd4\xc3\xb2\xa1', b'\xa1\xb2\xc3\xd4'):
        yield from _classic(data, magic)
    elif magic == b'\x0a\x0d\x0d\x0a':
        yield from _pcapng(data)
    else:
        raise SystemExit(f"unknown magic {magic.hex()}")

def _eth_ipv4_udp(pkt):
    if len(pkt) < 14: return None
    eth_type = struct.unpack('>H', pkt[12:14])[0]
    off = 14
    if eth_type == 0x8100:  # vlan
        eth_type = struct.unpack('>H', pkt[16:18])[0]; off = 18
    if eth_type != 0x0800: return None
    ip = pkt[off:]
    if len(ip) < 20: return None
    ihl = (ip[0] & 0x0f) * 4
    if ip[9] != 17: return None  # not UDP
    src = '.'.join(map(str, ip[12:16])); dst = '.'.join(map(str, ip[16:20]))
    udp = ip[ihl:]
    if len(udp) < 8: return None
    sport, dport, ulen, _ = struct.unpack('>HHHH', udp[:8])
    return src, sport, dst, dport, udp[8:]

def _classic(data, magic):
    endi = '<' if magic == b'\xd4\xc3\xb2\xa1' else '>'
    # global header 24 bytes; network type at offset 20
    linktype = struct.unpack(endi+'I', data[20:24])[0]
    off = 24
    while off + 16 <= len(data):
        ts_sec, ts_usec, incl, orig = struct.unpack(endi+'IIII', data[off:off+16])
        off += 16
        pkt = data[off:off+incl]; off += incl
        ts = ts_sec + ts_usec/1e6
        if linktype == 1:
            r = _eth_ipv4_udp(pkt)
        elif linktype == 0:  # null/loopback: 4-byte family then IP
            r = _eth_ipv4_udp(b'\x00'*12 + b'\x08\x00' + pkt[4:])
        else:
            r = _eth_ipv4_udp(pkt)
        if r: yield (ts,)+r

def _pcapng(data):
    off = 0; linktype = 1; endi = '<'
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from('<II', data, off)
        if blen == 0 or off+blen > len(data): break
        body = data[off+8:off+blen-4]
        if btype == 0x00000001:  # IDB
            linktype = struct.unpack_from('<H', body, 0)[0]
        elif btype == 0x00000006:  # EPB
            _, hi, lo, caplen, _orig = struct.unpack_from('<IIIII', body, 0)
            pkt = body[20:20+caplen]
            ts = ((hi<<32)|lo)/1e6
            r = _eth_ipv4_udp(pkt)
            if r: yield (ts,)+r
        elif btype == 0x00000003:  # simple packet
            caplen = struct.unpack_from('<I', body, 0)[0]
            pkt = body[4:4+caplen]
            r = _eth_ipv4_udp(pkt)
            if r: yield (0,)+r
        off += blen

if __name__ == '__main__':
    path = sys.argv[1]
    DEV = '192.168.16.200'
    # Focus: PC->Device port 50002 (TX IQ) and 50001 (control)
    sub_hist = collections.Counter()
    flag_hist = collections.Counter()
    to_dev_50002 = []
    to_dev_50001 = []
    n = 0
    for ts, src, sp, dst, dp, pl in read_pcap(path):
        n += 1
        if dst == DEV and dp == 50002:
            to_dev_50002.append((ts, len(pl), pl[:16]))
            if len(pl) >= 4:
                sub = struct.unpack('<H', pl[2:4])[0]
                sub_hist[hex(sub)] += 1
                if len(pl) >= 10:
                    fl = struct.unpack('<H', pl[8:10])[0]
                    flag_hist[(hex(sub), hex(fl), len(pl))] += 1
        elif dst == DEV and dp == 50001:
            to_dev_50001.append((ts, len(pl), pl))
    print(f"total UDP packets: {n}")
    print(f"PC->Dev:50002 count: {len(to_dev_50002)}")
    print(f"PC->Dev:50001 count: {len(to_dev_50001)}")
    print("\n== :50002 sub-type histogram ==")
    for k,v in sub_hist.most_common(): print(f"  sub={k}: {v}")
    print("\n== :50002 (sub, flags, len) histogram ==")
    for k,v in flag_hist.most_common(20): print(f"  sub={k[0]} flags={k[1]} len={k[2]}: {v}")

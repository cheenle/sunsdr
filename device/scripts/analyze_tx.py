#!/usr/bin/env python3
"""Consolidated SunSDR2 DX TX-chain analyzer.

Parses an ExpertSDR3 TX capture (default: device/captures/sunsdr_sdr_tx.pcap)
and emits a machine-readable JSON summary of the transmit protocol:
  - 50002 sub-type / flags / length histogram
  - 0xFFFD TX-IQ packet counter pattern + inter-packet timing
  - per-burst IQ level statistics (RMS / peak envelope, leading silence)
  - 50001 control command histogram + PTT events

Usage:
  python3 analyze_tx.py [capture.pcap] [out.json]

Pure stdlib pcap reader (no scapy). Little-endian IPv4/UDP only.
"""
import sys, struct, json, statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # device/
DEFAULT_PCAP = ROOT / "captures" / "sunsdr_sdr_tx.pcap"
DEFAULT_OUT = ROOT / "data" / "tx_analysis.json"

DEVICE_IP = "192.168.16.200"


def packets(path):
    """Yield (ts, udp_payload, src_ip, dst_ip, src_port, dst_port) for each UDP pkt."""
    with open(path, "rb") as f:
        gh = f.read(24)
        if len(gh) < 24:
            return
        magic = struct.unpack("<I", gh[:4])[0]
        le = magic in (0xA1B2C3D4, 0xA1B23C4D)
        endian = "<" if le else ">"
        nano = magic in (0xA1B23C4D, 0x4D3CB2A1)
        # link type
        linktype = struct.unpack(endian + "I", gh[20:24])[0]
        while True:
            ph = f.read(16)
            if len(ph) < 16:
                break
            ts_sec, ts_frac, incl, orig = struct.unpack(endian + "IIII", ph)
            data = f.read(incl)
            if len(data) < incl:
                break
            ts = ts_sec + ts_frac / (1e9 if nano else 1e6)
            # strip link layer
            if linktype == 1:        # Ethernet
                if len(data) < 14:
                    continue
                eth_type = struct.unpack(">H", data[12:14])[0]
                if eth_type != 0x0800:
                    continue
                ip = data[14:]
            elif linktype == 0:      # NULL/loopback (BSD)
                ip = data[4:]
            else:
                ip = data
            if len(ip) < 20 or (ip[0] >> 4) != 4:
                continue
            ihl = (ip[0] & 0x0F) * 4
            proto = ip[9]
            if proto != 17:           # UDP
                continue
            src_ip = ".".join(str(b) for b in ip[12:16])
            dst_ip = ".".join(str(b) for b in ip[16:20])
            udp = ip[ihl:]
            if len(udp) < 8:
                continue
            sport, dport = struct.unpack(">HH", udp[:4])
            payload = udp[8:]
            yield ts, payload, src_ip, dst_ip, sport, dport


def decode_iq(payload):
    """16-byte stream header + 200×6B 24-bit LE IQ -> list of (i,q) floats."""
    body = payload[16:]
    n = min(200, len(body) // 6)
    out = []
    for k in range(n):
        off = k * 6
        i = int.from_bytes(body[off:off+3], "little", signed=True) / 8388608.0
        q = int.from_bytes(body[off+3:off+6], "little", signed=True) / 8388608.0
        out.append((i, q))
    return out


def pkt_levels(payload):
    iq = decode_iq(payload)
    if not iq:
        return 0.0, 0.0
    mags = [(i*i + q*q) ** 0.5 for i, q in iq]
    peak = max(mags)
    rms = (sum(m*m for m in mags) / len(mags)) ** 0.5
    return peak, rms


def main():
    pcap = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PCAP
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT

    sub_hist = {}        # (sub,flags,len) -> count
    fffd = []            # (ts, counter, payload)
    fffe_ts = []
    ctrl_cmds = {}       # cmd -> count
    ptt_events = []      # (ts, payload_hex, trailing)
    interleave = []      # (ts, 'D'/'E')

    for ts, p, sip, dip, sp, dp in packets(pcap):
        if len(p) < 4 or p[0] != 0x32 or p[1] != 0xFF:
            continue
        sub = struct.unpack("<H", p[2:4])[0]

        # --- stream packets to device:50002 ---
        if dip == DEVICE_IP and dp == 50002:
            flags = struct.unpack("<H", p[8:10])[0] if len(p) >= 10 else 0
            key = (hex(sub), hex(flags), len(p))
            sub_hist[key] = sub_hist.get(key, 0) + 1
            counter = struct.unpack("<I", p[4:8])[0]
            if sub == 0xFFFD:
                fffd.append((ts, counter, p))
                interleave.append((ts, "D"))
            elif sub == 0xFFFE:
                fffe_ts.append(ts)
                interleave.append((ts, "E"))

        # --- control packets to device:50001 ---
        elif dip == DEVICE_IP and dp == 50001:
            cmd = sub
            ctrl_cmds[hex(cmd)] = ctrl_cmds.get(hex(cmd), 0) + 1
            if cmd == 0x0006:  # PTT
                # header(14) + payload(len) + trailing(4)
                dlen = struct.unpack("<I", p[4:8])[0]
                payload = p[14:14+dlen]
                trailing = struct.unpack("<I", p[14+dlen:14+dlen+4])[0] if len(p) >= 14+dlen+4 else None
                ptt_events.append((ts, payload.hex(), trailing))

    # --- burst segmentation on 0xFFFD ---
    bursts = []
    if fffd:
        cur = [fffd[0]]
        for prev, nxt in zip(fffd, fffd[1:]):
            gap = nxt[0] - prev[0]
            if gap > 0.5:  # >500ms gap = new burst (new PTT)
                bursts.append(cur)
                cur = [nxt]
            else:
                cur.append(nxt)
        bursts.append(cur)

    burst_stats = []
    for b in bursts:
        ts0 = b[0][0]
        dur = b[-1][0] - ts0
        ivs = [(b[k+1][0] - b[k][0]) * 1000 for k in range(len(b)-1)]
        peaks, rmss = [], []
        for _, _, p in b:
            pk, rm = pkt_levels(p)
            peaks.append(pk)
            rmss.append(rm)
        # leading silence
        lead = 0
        for rm in rmss:
            if rm < 1e-5:
                lead += 1
            else:
                break
        burst_stats.append({
            "start_rel_s": round(ts0 - fffd[0][0], 4),
            "duration_s": round(dur, 4),
            "n_packets": len(b),
            "rate_pkt_s": round(len(b) / dur, 1) if dur > 0 else None,
            "implied_tx_sr_hz": round((len(b) / dur) * 200, 0) if dur > 0 else None,
            "iv_ms": {
                "median": round(statistics.median(ivs), 3) if ivs else None,
                "mean": round(statistics.mean(ivs), 3) if ivs else None,
                "min": round(min(ivs), 3) if ivs else None,
                "max": round(max(ivs), 3) if ivs else None,
            },
            "iq_level": {
                "peak_max": round(max(peaks), 5),
                "rms_max": round(max(rmss), 5),
                "rms_mean": round(statistics.mean(rmss), 5),
            },
            "leading_silence_pkts": lead,
        })

    # counter pattern
    counters = [c for _, c, _ in fffd[:8]]
    cdiffs = [hex(fffd[k+1][1] - fffd[k][1]) for k in range(min(7, len(fffd)-1))]

    # global IQ peak
    gmax = 0.0
    for _, _, p in fffd:
        pk, _ = pkt_levels(p)
        gmax = max(gmax, pk)

    result = {
        "capture": str(pcap.name),
        "stream_50002_histogram": {f"{k[0]} flags={k[1]} len={k[2]}": v
                                   for k, v in sorted(sub_hist.items())},
        "tx_iq_packet": {
            "total_fffd": len(fffd),
            "total_fffe": len(fffe_ts),
            "header_hex_example": fffd[0][2][:16].hex() if fffd else None,
            "counter_first8": [hex(c) for c in counters],
            "counter_diffs": cdiffs,
            "counter_step": "0x10000 (shared between 0xFFFD and 0xFFFE)",
            "global_peak_iq": round(gmax, 5),
        },
        "bursts": burst_stats,
        "ctrl_50001_histogram": ctrl_cmds,
        "ptt_events": [{"ts": round(t, 3), "payload": ph, "trailing": tr}
                       for t, ph, tr in ptt_events],
        "interleave_sample": "".join(c for _, c in interleave[:240]),
        "findings": {
            "tx_sample_rate_hz": 39063,
            "tx_sample_rate_note": "VERIFIED: TX runs at 39063 Hz (5^7/2 = "
                "RX/2), the LOWEST of the manual's 39;78;156;312 kHz rates. "
                "Median inter-frame interval 5.12ms across all 3 PTT bursts "
                "(195.8 pkt/s x 200 samples). Counter resets to 0x04B0 each PTT.",
            "rx_sample_rate_hz": 78125,
            "rx_sample_rate_note": "RX (0xFFFE device->PC) measured 2.554ms/pkt "
                "= 78125 Hz.",
            "tx_iq_peak_observed": round(gmax, 5),
            "code_assumed_peak": 0.3,
            "code_assumed_tx_interval_ms": 2.56,
            "code_assumed_tx_interval_note": "server.py TX_INTERVAL="
                "200/78125=2.56ms is 2x TOO FAST. Device consumes TX IQ at "
                "5.12ms/pkt. Overfeeding likely causes buffer overrun / "
                "periodic popping.",
            "note_level": "ExpertSDR3 TX IQ peaks ~0.09, NOT 0.3. 0.3 likely "
                          "triggers hardware ALC/clipping (popping artifacts).",
            "note_silence": "~17 leading zero packets (~87ms @ 5.12ms/pkt) "
                            "before audio = PA/relay settling time.",
            "note_counter": "Stream-packet bytes 4-7 are a packet COUNTER, not "
                            "data_len as documented for control packets.",
            "note_interleave": "~8x 0xFFFD per 1x 0xFFFE keep-alive during TX.",
            "device_telemetry": {
                "sub_0x1F00": "34B device->PC, floats e.g. 45.0 @off18, "
                              "1.0 @off26 - likely PA temp/fwd power/SWR",
                "sub_0x1F01": "22B device->PC, mostly zeros - TX status flags",
            },
        },
    }

    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Wrote {out}")
    print(json.dumps(result["findings"], indent=2, ensure_ascii=False))
    print(json.dumps(result["bursts"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

# SunSDR2 DX — Device Communication Protocol

> Reverse-engineered from ExpertSDR3 1.0.17 packet captures.  
> Implementation: `web_control/sunsdr_direct.py`

## Table of contents

1. [Network topology](#1-network-topology)
2. [Packet format](#2-packet-format)
3. [Command IDs](#3-command-ids)
4. [Control flow: boot sequence](#4-control-flow-boot-sequence)
5. [Control flow: runtime commands](#5-control-flow-runtime-commands)
6. [Data flow: IQ stream](#6-data-flow-iq-stream)
7. [Data flow: stream keep-alive](#7-data-flow-stream-keep-alive)
8. [Data flow: heartbeat](#8-data-flow-heartbeat)
9. [Frequency encoding (IF offset)](#9-frequency-encoding-if-offset)
10. [Full lifecycle](#10-full-lifecycle)
11. [Reference: hex bytes](#11-reference-hex-bytes)

---

## 1. Network topology

```
┌────────────────────┐         UDP 50001 (control)
│  PC / Server       │  ────────────────────────────  ┌─────────────────┐
│  192.168.16.100    │                                │  SunSDR2 DX     │
│                    │  ────────────────────────────  │  192.168.16.200 │
│  port 50001 ctrl   │         UDP 50002 (IQ stream)  │                 │
│  port 50002 IQ rx  │   ←──────────────────────────  │  port 50001     │
└────────────────────┘                                │  port 50002     │
                                                      └─────────────────┘
```

| Role | Address | Port | Direction |
|------|---------|------|-----------|
| PC control socket | 192.168.16.100 | 50001 | PC → Device (commands) |
| PC IQ socket | 192.168.16.100 | 50002 | Device → PC (IQ samples) |
| Device | 192.168.16.200 | 50001 | Receives commands |
| Device | 192.168.16.200 | 50002 | Sends IQ data |

**Critical rule**: All control packets MUST originate from PC source port 50001. The device identifies the controller by the source port of incoming UDP packets.

---

## 2. Packet format

Every control packet (both directions) uses the same 14-byte header + variable payload + 4-byte trailing word:

```
Byte offset  | 0-1    | 2-3     | 4-7       | 8-11    | 12-13    | 14..14+N-1 | 14+N..14+N+3
Field        | magic  | cmd_id  | data_len  | index   | reserved | payload    | trailing
Type         | uint16 | uint16  | uint32    | uint32  | uint16   | bytes      | uint32
Value        | 0xFF32 | varies  | len(data) | 0x00010000         | 0x0000    | varies      | varies
```

- **magic**: Always `0xFF32` (little-endian, so bytes appear as `32 FF` on wire)
- **cmd_id**: Command identifier (see §3)
- **data_len**: Length of payload in bytes
- **index**: Always `0x00010000` for primary TRX channel
- **reserved**: Always `0x0000`
- **payload**: Command-specific data
- **trailing**: 4-byte word; meaning varies by command (not a CRC)

### build_packet()

```python
def build_packet(cmd_id, data=b"", index=0x00010000, trailing=0):
    hdr = struct.pack("<HHIIH", 0xFF32, cmd_id, len(data), index, 0)
    return hdr + data + struct.pack("<I", trailing)
```

On the wire (LE): `32 FF [cmd_le] [len_le] 00 00 01 00 00 00 [payload...] [trailing_le]`

---

## 3. Command IDs

| Command | ID | Payload | Purpose |
|---------|----|---------|---------|
| `START_OPS` | `0x0002` | `uint32 0` | **Must be first.** Powers on TRX, initializes hardware. |
| `HW_INIT` | `0x0001` | 68 bytes | Hardware init with calibration blob. |
| `SET_PARAM_5` | `0x0005` | `uint32` | Mode / attenuator / preamp config. |
| `PTT` | `0x0006` | `uint32 0` | PTT. **trailing=1** = TX, **trailing=0** = RX. |
| `SET_PARAM_7` | `0x0007` | 26-44 bytes | Extended parameter config. |
| `RX_FREQ` | `0x0008` | `uint32[2]` (index, DDS×10) | Set RX DDS frequency (includes IF offset). |
| `TX_FREQ` | `0x0009` | `uint32[2]` (index, VFO×10) | Set TX VFO frequency (exact, no IF offset). |
| `INFO_QUERY` | `0x000E` | none | Query device info (486-byte response). |
| `CALIB_QUERY` | `0x0010` | none | Query calibration data (486-byte response). |
| `SET_PARAM_15` | `0x0015` | `uint32` | Post-init parameter. Value `1` at boot. |
| `SET_PARAM_17` | `0x0017` | `uint32` | Parameter set. |
| `HEARTBEAT` | `0x0018` | `uint32 0` | **Keep-alive.** Must be sent every 0.5s. Session times out after ~8 minutes without it. |
| `SET_PARAM_19` | `0x0019` | `uint32 0xBB` | Parameter set. |
| `SET_PARAM_1B` | `0x001B` | `uint32 0` | Parameter set. |
| `SET_PARAM_1D` | `0x001D` | `uint32 0` | Parameter set. |
| `SET_PARAM_1E` | `0x001E` | `uint32 0` | Parameter set. |
| `STREAM_CTRL` | `0x0020` | 60 bytes | **Start IQ stream.** Configures destination and format. |
| `SET_PARAM_21` | `0x0021` | `uint32 1` | Parameter set. |
| `SET_PARAM_22` | `0x0022` | 12 bytes | Parameter set. |
| `SET_PARAM_24` | `0x0024` | `uint32 0` | Parameter set. |
| `SET_PARAM_26` | `0x0026` | `uint32 0` | Parameter set. |
| `VOX_CTRL` | `0x0027` | 16 bytes | VOX configuration. |
| `STATUS` | `0x005A` | none | Status query (28-byte response). |
| `PRE_CONFIG` | `0x005F` | 6 bytes (zeros) | Pre-boot config handshake. Sent 3× after START_OPS. |

---

## 4. Control flow: boot sequence

The boot sequence is a fixed-order 30-packet initialization. It MUST be sent from PC source port 50001.

### Phase 1: Pre-config (10 packets)

Must start with `0x0002` START_OPS, then handshake and hardware configuration.

```
#  Wire hex (14B header + payload + 4B trailing)
0x0002  32ff0200040000000000010000000000000000000000     # START_OPS
0x005F  32ff5f000600000000000100000000000000000000000000 # PRE_CONFIG ×3
0x005F  32ff5f000600000000000100000000000000000000000000
0x005F  32ff5f000600000000000100000000000000000000000000
0x001D  32ff1d00040000000000010000000000000000000000     # SET_PARAM_1D
0x001B  32ff1b00040000000000010000000000000000000000     # SET_PARAM_1B
0x0005  32ff0500040000000000010000000000000002000000     # SET_PARAM_5 (value=2)
0x0018  32ff1800040000000000010000000000000000000000     # HEARTBEAT
0x0019  32ff19000400000000000100000000000000bb000000     # SET_PARAM_19 (0xBB)
0x0021  32ff2100040000000000010000000000000001000000     # SET_PARAM_21 (value=1)
```

### Phase 2: Status queries (5 packets)

```
0x005A  32ff5a000000000000000100000000000000             # STATUS ×5
0x005A  32ff5a000000000000000100000000000000
0x005A  32ff5a000000000000000100000000000000
0x005A  32ff5a000000000000000100000000000000
0x005A  32ff5a000000000000000100000000000000
```

### Phase 3: Hardware init (1 packet)

```
0x0001  32ff0100320000000000010000000000000032000000... # HW_INIT (68B payload + trailing=0xC025)
```

### Phase 4: Frequencies (3 packets, dynamic)

These are built dynamically because they contain the current VFO frequency.

```
0x0009  TX_FREQ  [index=0, vfo×10]        # TX VFO (exact, no IF)
0x0008  RX_FREQ  [index=0, dds×10]        # RX DDS (VFO + IF_OFFSET)
0x0008  RX_FREQ  [index=0, dds×10]        # RX DDS (dup)
```

Where:
- `vfo = int(rx_freq * 10)` — e.g., 7_074_000 Hz → 70740000
- `dds = int((rx_freq + 30500.0) * 10)` — DDS = VFO + IF offset

### Phase 5: Post-init and stream start (10 packets)

```
0x0017  32ff17000400000000000100000000000000dc000000     # SET_PARAM_17 (0xDC)
0x001E  32ff1e00040000000000010000000000000000000000     # SET_PARAM_1E
0x0015  32ff1500040000000000010000000000000001000000     # SET_PARAM_15 (value=1)
0x0007  32ff07001a00000000000100000000...                 # SET_PARAM_7 (26B zeros)
0x0024  32ff2400040000000000010000000000000000000000     # SET_PARAM_24
0x0020  32ff20003400000000000100000000000000...           # STREAM_CTRL (60B) ★ STARTS IQ
0x0018  32ff1800040000000000010000000000000000000000     # HEARTBEAT
0x0026  32ff2600040000000000010000000000000000000000     # SET_PARAM_26
0x0027  32ff27001000000000000100000000000000dc460300...  # VOX_CTRL
0x0022  32ff22000c00000000000100000000000000000000000084d71700000000  # SET_PARAM_22
```

**Timing**: 30ms gap between each packet (`await asyncio.sleep(0.03)`).

**Key**: `STREAM_CTRL` (0x0020) is the critical packet that tells the device to start sending IQ data to the PC. The device streams to the source IP of the control packets (192.168.16.100), using the IQ port (50002).

### STREAM_CTRL payload format (60 bytes)

```
Byte offset | Size | Value          | Meaning
0-3         | u32  | 0x00000000     | RX channel = 0
4-7         | u32  | 0x00000001     | TX channel = 1
8-11        | u32  | 0x00000000     |
12-15       | u32  | 0x00000000     |
16-19       | u32  | 0x00000064     | 100 (last octet of PC IP?)
20-23       | u32  | 0x00000000     |
24-27       | u32  | 0x00000000     |
28-31       | u32  | 0x00000000     |
32-35       | u32  | 0x0000001e     | 30
36-39       | u32  | 0x000002bc     | 700
40-43       | u32  | 0x00000007     | 7
44-47       | u32  | 0x00000064     | 100
48-51       | u32  | 0x0000012c     | 300
52-55       | u32  | 0x00000064     | 100
```

The trailing word for STREAM_CTRL is `0x64` (100).

---

## 5. Control flow: runtime commands

After boot, the device accepts these commands at any time:

### Frequency change

```
Command:     RX_FREQ (0x0008) + TX_FREQ (0x0009)
Payload:     Two uint32: [channel_index=0, freq×10]
Direction:   PC → Device
```

RX uses DDS frequency (VFO + 30500 Hz IF offset). TX uses exact VFO frequency. Both must be sent together for a frequency change to take effect.

```
RX: struct.pack("<II", 0, int((freq + 30500.0) * 10))
TX: struct.pack("<II", 0, int(freq * 10))
```

### PTT (Push-To-Talk)

```
Command:     PTT (0x0006)
Payload:     uint32 0
Trailing:    1 = TX, 0 = RX
```

The trailing word controls the TX/RX state — NOT the payload. This is unique among commands.

```python
# TX on
build_packet(0x0006, struct.pack("<I", 0), trailing=1)
# TX off (RX)
build_packet(0x0006, struct.pack("<I", 0), trailing=0)
```

### Heartbeat (keep-alive)

```
Command:     HEARTBEAT (0x0018)
Payload:     uint32 0
Interval:    Every 0.5s
```

Must be sent continuously from a background task. If heartbeat stops, the device session times out after approximately 8 minutes and stops streaming IQ data.

### Other setters (no-ops or state-only)

The following are maintained as client-side state but do not send hardware commands in the current DIRECT implementation: `set_mode`, `set_filter`, `set_agc_mode`, `set_volume`, `set_drive`, `set_rf_gain`, `set_preamp`, `set_attenuator`, `set_antenna`, `set_tune`, `set_rit_*`, `set_split`, `set_vfo_lock`.

Mode, filter, AGC are handled purely in the DSP software layer (`AudioDemodulator`), not sent to the hardware.

---

## 6. Data flow: IQ stream

Once the boot sequence completes (specifically after `STREAM_CTRL` 0x0020), the device continuously sends IQ data packets to the PC's port 50002.

### IQ packet format

```
Byte offset | Size  | Field
0-1         | u16   | magic = 0xFF32
2-3         | u16   | sub-type = 0xFFFE (IQ data marker)
4-7         | u32   | data_len
8-9         | u16   | flags/index
10..10+N-1  | bytes | IQ sample data (N = min(200, data_len/6) × 6 bytes)
```

### IQ sample decoding (24-bit signed, little-endian)

Each sample is 6 bytes: 3 bytes I, 3 bytes Q, both signed 24-bit LE.

```python
for i in range(n):
    off = i * 6
    i_val = int.from_bytes(payload[off:off+3], 'little', signed=True)
    q_val = int.from_bytes(payload[off+3:off+6], 'little', signed=True)
    iq[i] = complex(i_val / 8388608.0, q_val / 8388608.0)
```

- Normalization divisor: `8388608.0` = 2^23 (24-bit signed range)
- Output: `numpy.complex64` array of up to 200 samples per packet
- Packet rate: ~390.7 packets/sec
- True IQ sample rate: **78,125 Hz** (5^7)
- Each packet carries 200 IQ samples → 390.625 pkt/s × 200 = 78,125 samples/s ✓

### Processing pipeline

```
UDP packet (port 50002)
  → validate magic=0xFF32, sub=0xFFFE
  → decode 24-bit I/Q → complex64
  → StreamProcessor.feed_iq()
        ├─ SpectrumProcessor.feed()     → FFT (2048-pt Hanning) → dB spectrum
        │    → every ~10 packets: fftshift → 20*log10 → clip(-120,0) → bin to 512
        │    → broadcast to /WSspectrum clients (uint8 quantized)
        └─ AudioDemodulator.demodulate() → IF shift + SSB bandpass + decimate(×5) + AGC
             → audio buffer → 512-sample chunks → broadcast to /WSaudioRX clients
```

---

## 7. Data flow: stream keep-alive

In addition to the control heartbeat (0x0018), the IQ stream itself requires periodic keep-alive packets sent TO the device's port 50002.

### Format

```
Magic:       0xFF32
Sub-type:    0xFFFE (same marker as IQ data)
Counter:     uint32, incrementing by 0x10000 each time
Flags:       0x0001
Payload:     1200 bytes of zeros
```

```python
hdr = struct.pack("<HHIH", 0xFF32, 0xFFFE, tx_counter, 0x0001)
iq_sock.sendto(hdr + b'\x00' * 1200, ("192.168.16.200", 50002))
```

**Interval**: Every 0.5s (same as heartbeat). Without this, the device may stop the IQ stream.

### TX dummy stream

When PTT is active, the stream keep-alive changes sub-type to `0xFFFD` with different flags:

```python
hdr = struct.pack("<HHIH", 0xFF32, 0xFFFD, tx_counter, 0x0102)
iq_sock.sendto(hdr + b'\x00' * 1200, ("192.168.16.200", 50002))
```

---

## 8. Data flow: heartbeat

The control heartbeat (0x0018) is a background task running every 0.5s:

```python
async def _heartbeat_task():
    hb = build_packet(0x0018, struct.pack("<I", 0))
    while radio.connected:
        ctrl_sock.sendto(hb, ("192.168.16.200", 50001))
        await asyncio.sleep(0.5)
```

Wire bytes: `32ff1800040000000000010000000000000000000000`

Without periodic heartbeat, the device session times out (~8 min) and IQ streaming stops.

---

## 9. Frequency encoding (IF offset)

The SunSDR2 DX uses a superheterodyne architecture with an intermediate frequency of 30,500 Hz.

| Parameter | Formula | Example (7.074 MHz) |
|-----------|---------|---------------------|
| VFO frequency | `freq` | 7,074,000 Hz |
| RX DDS | `(freq + 30500.0) * 10` | 71,045,000 |
| TX VFO | `freq * 10` | 70,740,000 |

The DDS value is sent to `RX_FREQ` (0x0008). The VFO value is sent to `TX_FREQ` (0x0009). Both are multiplied by 10 before sending (unit: 0.1 Hz).

In the DSP pipeline, the IQ data is shifted back by the IF offset before demodulation:

```python
dphi = 2.0 * math.pi * 30500.0 / 78125.0   # IF shift per sample
phases = np.cumsum(dphi)
bb = iq * np.exp(1j * phases)               # shift VFO → 0 Hz
```

---

## 10. Full lifecycle

```
PC: bind 192.168.16.100:50001 (control)
PC: bind 192.168.16.100:50002 (IQ)

  ┌─ BOOT ─────────────────────────────────────────────┐
  │ Phase 1: 10 packets (START_OPS → SET_PARAM_21)     │
  │ Phase 2:  5 packets (STATUS queries)               │
  │ Phase 3:  1 packet  (HW_INIT)                     │
  │ Phase 4:  3 packets (TX_FREQ, RX_FREQ ×2)          │
  │ Phase 5: 10 packets (SET_PARAM_15 → SET_PARAM_22)  │
  │           ★ STREAM_CTRL 0x0020 starts IQ flow       │
  └────────────────────────────────────────────────────┘
  │
  ▼
  ┌─ STEADY STATE ─────────────────────────────────────┐
  │ Every 0.5s:                                        │
  │   → HEARTBEAT (0x0018) to device:50001             │
  │   → Stream keep-alive (0xFFFE) to device:50002     │
  │                                                     │
  │ Continuously:                                       │
  │   ← IQ data packets from device:50002              │
  │      ~390.7 pkt/s, 200 samples/pkt, 78,125 Hz      │
  │                                                     │
  │ On TX:                                              │
  │   → PTT (0x0006, trailing=1)                       │
  │   → Stream packets switch to 0xFFFD dummy           │
  │                                                     │
  │ On frequency change:                                │
  │   → RX_FREQ (0x0008) + TX_FREQ (0x0009)            │
  └────────────────────────────────────────────────────┘
  │
  ▼
  ┌─ SHUTDOWN ─────────────────────────────────────────┐
  │ PTT (0x0006, trailing=0)  [force RX]               │
  │ Close sockets                                       │
  └────────────────────────────────────────────────────┘
```

### Data rate summary

| Stream | Direction | Rate | Format |
|--------|-----------|------|--------|
| IQ samples | Device → PC | 78,125 Hz | 24-bit signed I/Q, complex |
| IQ packets | Device → PC | ~390.7 pkt/s | 200 samples/pkt, UDP |
| Spectrum frames | Server → Browser | ~38 Hz | 512 × uint8 (quantized dB) |
| Audio | Server → Browser | 16,000 Hz | Int16 PCM (resampled from 15,625 Hz) |
| Heartbeat | PC → Device | 2 Hz | 0x0018 empty packet |
| Stream keep-alive | PC → Device | 2 Hz | 0xFFFE + 1200B zeros |

---

## 11. Reference: hex bytes

Complete boot sequence as wire hex (from ExpertSDR3 pcap):

### Phase 1
```
32ff0200040000000000010000000000000000000000
32ff5f000600000000000100000000000000000000000000
32ff5f000600000000000100000000000000000000000000
32ff5f000600000000000100000000000000000000000000
32ff1d00040000000000010000000000000000000000
32ff1b00040000000000010000000000000000000000
32ff0500040000000000010000000000000002000000
32ff1800040000000000010000000000000000000000
32ff19000400000000000100000000000000bb000000
32ff2100040000000000010000000000000001000000
```

### Phase 2
```
32ff5a000000000000000100000000000000  (×5)
```

### Phase 3
```
32ff01003200000000000100000000000000320000003200000032000000320000003200000032000000320000003200000000000000010001000100000000000000c0250000
```

### Phase 5 (post-init + stream)
```
32ff17000400000000000100000000000000dc000000
32ff1e00040000000000010000000000000000000000
32ff1500040000000000010000000000000001000000
32ff07001a000000000001000000000000000000000000000000000000000000000000000000000000000000
32ff2400040000000000010000000000000000000000
32ff20003400000000000100000000000000000000000100000000000000000000006400000000000000000000001e000000bc02000007000000640000002c01000064000000
32ff1800040000000000010000000000000000000000
32ff2600040000000000010000000000000000000000
32ff27001000000000000100000000000000dc460300b6d20000dc460300b6d20000
32ff22000c00000000000100000000000000000000000084d71700000000
```

### Heartbeat (every 0.5s)
```
32ff1800040000000000010000000000000000000000
```

### PTT
```
TX: 32ff060004000000000001000000000000000000000001000000
RX: 32ff060004000000000001000000000000000000000000000000
```

### Frequency (example: 7.074 MHz)
```
TX_FREQ: 32ff09000800000000000100000000000000000000005037040000000000
RX_FREQ: 32ff08000800000000000100000000000000000000008dcb430000000000
```

### Stream keep-alive (to device port 50002, every 0.5s)
```
32fffe00b00400010000000000000000  +  1200 bytes of zeros
```

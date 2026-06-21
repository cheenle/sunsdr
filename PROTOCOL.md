# SunSDR2 DX — Complete Communication & Processing Protocol

> Reverse-engineered from ExpertSDR3 1.0.17 packet captures.  
> Implementations: `web_control/sunsdr_direct.py`, `web_control/dsp.py`, `sunmrrc/server.py`

---

## Table of Contents

1. [Network Topology](#1-network-topology)
2. [UDP Control Packet Format](#2-udp-control-packet-format)
3. [Command Reference](#3-command-reference)
4. [Boot Sequence](#4-boot-sequence)
5. [Runtime Control Commands](#5-runtime-control-commands)
6. [IQ Stream Data Flow](#6-iq-stream-data-flow)
7. [DSP Pipeline](#7-dsp-pipeline)
8. [WDSP Integration](#8-wdsp-integration)
9. [WebSocket Protocol](#9-websocket-protocol)
10. [S-Meter & Spectrum Broadcasting](#10-s-meter--spectrum-broadcasting)
11. [Audio Broadcasting](#11-audio-broadcasting)
12. [ATR-1000 Protocol](#12-atr-1000-protocol)
13. [Frontend Data Processing](#13-frontend-data-processing)
14. [Full Lifecycle](#14-full-lifecycle)
15. [Data Rate Summary](#15-data-rate-summary)
16. [Hex Reference](#16-hex-reference)
17. [TX Chain — Verified from Capture](#17-tx-chain--verified-from-capture)
17. [TX Chain — Verified from Capture](#17-tx-chain--verified-from-capture)
17. [TX Chain — Verified from Capture](#17-tx-chain--verified-from-capture)

---

## 1. Network Topology

```
┌──────────────────────────┐                          ┌─────────────────────┐
│  PC / Server             │     UDP 50001 (ctrl)     │  SunSDR2 DX         │
│  192.168.16.100          │ ───────────────────────→ │  192.168.16.200      │
│                          │                          │                     │
│  :50001  control socket  │     UDP 50002 (IQ rx)    │  :50001  ctrl port  │
│  :50002  IQ stream rx    │ ←─────────────────────── │  :50002  stream out │
│  :8889   HTTPS/WSS       │                          │                     │
└──────────────────────────┘                          └─────────────────────┘
         ↑ HTTPS/WSS
    ┌─────────┐
    │ Browser │  (iPhone Safari / desktop)
    └─────────┘
```

| Role | Address | Port | Protocol | Direction |
|------|---------|------|----------|-----------|
| PC control | 192.168.16.100 | 50001 | UDP | PC → Device |
| PC IQ | 192.168.16.100 | 50002 | UDP | Device → PC |
| PC keep-alive | 192.168.16.100 | 50002 | UDP | PC → Device |
| Web server | :: (all) | 8889 | HTTPS/WSS | Browser ↔ Server |
| Device ctrl | 192.168.16.200 | 50001 | UDP | ← PC / → PC (responses) |
| Device stream | 192.168.16.200 | 50002 | UDP | → PC (IQ samples) |

**Critical rule**: All control packets MUST originate from PC source port 50001. The device identifies the active controller by the source port.

### Hardcoded values

```python
# sunsdr_direct.py
DEVICE_HOST = "192.168.16.200"
LOCAL_HOST  = "192.168.16.100"
CTRL_PORT   = 50001
IQ_PORT     = 50002
IF_OFFSET   = 30500.0
SESSION_ID  = 0x04B0

# dsp.py
IQ_SAMPLE_RATE = 78125    # 5^7, verified from pcap
AUDIO_DECIM    = 5
AUDIO_RATE     = 15625    # IQ_SAMPLE_RATE / AUDIO_DECIM
FFT_SIZE       = 2048
```

---

## 2. UDP Control Packet Format

Every control packet (both directions) uses an identical 14-byte header + variable payload + 4-byte trailing word:

```
Byte offset  | 0-1    | 2-3     | 4-7       | 8-11        | 12-13    | 14..14+N-1 | 14+N..14+N+3
Field        | magic  | cmd_id  | data_len  | index       | reserved | payload    | trailing
C type       | uint16 | uint16  | uint32    | uint32      | uint16   | bytes      | uint32
LE value     | 0xFF32 | varies  | len(data) | 0x00010000  | 0x0000   | varies     | varies
```

On the wire (little-endian): `32 FF [cmd_le] [len_le] 00 00 01 00 00 00 [payload...] [trailing_le]`

- **magic**: Always `0xFF32`. On wire bytes appear as `32 FF`.
- **cmd_id**: Command identifier from the table below.
- **data_len**: Number of payload bytes (can be 0).
- **index**: Always `0x00010000` for the primary (and only) TRX channel.
- **reserved**: Always `0x0000`.
- **payload**: Command-specific data; length = data_len.
- **trailing**: 4-byte word. **Not a CRC.** Meaning varies by command (e.g., PTT uses it for TX/RX state, STREAM_CTRL uses it for configuration value 0x64).

### Python builder

```python
def build_packet(cmd_id: int, data: bytes = b"",
                 index: int = 0x00010000,
                 trailing: int = 0) -> bytes:
    hdr = struct.pack("<HHIIH", 0xFF32, cmd_id, len(data), index, 0)
    return hdr + data + struct.pack("<I", trailing)
```

Minimum packet size: 14 (header) + 0 (payload) + 4 (trailing) = **18 bytes**.  
Maximum tested payload: 68 bytes (HW_INIT).

---

## 3. Command Reference

### 3.1 Complete command ID table

| ID | Name | Payload size | Trailing | Direction | Purpose |
|----|------|-------------|----------|-----------|---------|
| `0x0001` | HW_INIT | 68 bytes | `0xC025` | PC→Dev | Hardware init with calibration blob |
| `0x0002` | START_OPS | 4 (`uint32 0`) | 0 | PC→Dev | **Must be first.** Power-on TRX. |
| `0x0005` | SET_PARAM_5 | 4 (`uint32`) | 0 | PC→Dev | Mode / attenuator / preamp config. Value `2` at boot. |
| `0x0006` | PTT | 4 (`uint32 0`) | **1=TX, 0=RX** | PC→Dev | Push-to-talk. Trail word controls state. |
| `0x0007` | SET_PARAM_7 | 26 bytes | 0 | PC→Dev | Extended config (all zeros at boot). |
| `0x0008` | RX_FREQ | 8 (`uint32×2`) | 0 | PC→Dev | RX DDS freq = (VFO + IF_OFFSET) × 10 |
| `0x0009` | TX_FREQ | 8 (`uint32×2`) | 0 | PC→Dev | TX VFO freq = VFO × 10 (exact, no IF) |
| `0x000E` | INFO_QUERY | 0 | 0 | PC→Dev | Device info query. **486-byte response.** |
| `0x0010` | CALIB_QUERY | 0 | 0 | PC→Dev | Calibration data query. **486-byte response.** |
| `0x0015` | SET_PARAM_15 | 4 (`uint32`) | 0 | PC→Dev | Post-init param. Value `1` at boot. |
| `0x0017` | SET_PARAM_17 | 4 (`uint32`) | 0 | PC→Dev | Parameter set. Value `0xDC` at boot. |
| `0x0018` | HEARTBEAT | 4 (`uint32 0`) | 0 | PC→Dev | **Keep-alive every 0.5s.** ~8 min session timeout. |
| `0x0019` | SET_PARAM_19 | 4 (`uint32`) | 0 | PC→Dev | Parameter set. Value `0xBB` at boot. |
| `0x001B` | SET_PARAM_1B | 4 (`uint32 0`) | 0 | PC→Dev | Parameter set. |
| `0x001D` | SET_PARAM_1D | 4 (`uint32 0`) | 0 | PC→Dev | Parameter set. |
| `0x001E` | SET_PARAM_1E | 4 (`uint32 0`) | 0 | PC→Dev | Parameter set. |
| `0x0020` | STREAM_CTRL | 60 bytes | `0x64` | PC→Dev | **Start IQ stream.** Configures destination + format. |
| `0x0021` | SET_PARAM_21 | 4 (`uint32 1`) | 0 | PC→Dev | Parameter set. |
| `0x0022` | SET_PARAM_22 | 12 bytes | 0 | PC→Dev | Parameter set. |
| `0x0024` | SET_PARAM_24 | 4 (`uint32 0`) | 0 | PC→Dev | Parameter set. |
| `0x0026` | SET_PARAM_26 | 4 (`uint32 0`) | 0 | PC→Dev | Parameter set. |
| `0x0027` | VOX_CTRL | 16 bytes | 0 | PC→Dev | VOX config. |
| `0x005A` | STATUS | 0 | 0 | PC→Dev | Status query. **18-28 byte response.** |
| `0x005F` | PRE_CONFIG | 6 bytes (zeros) | 0 | PC→Dev | Pre-boot handshake. Sent 3× after START_OPS. |

### 3.2 Response commands

The device responds to queries on port 50001:

| Trigger | Response cmd_id | Response size | Content |
|---------|----------------|---------------|---------|
| STATUS (0x005A) | 0x0002 (echoes START_OPS) | 18 bytes | Acknowledgement |
| INFO_QUERY (0x000E) | varies | 28-486 bytes | Device info / serial |
| CALIB_QUERY (0x0010) | varies | 486 bytes | Calibration data |

Example STATUS response: `32ff0200040000000000010000000000000000000000`

---

## 4. Boot Sequence

The boot sequence is a fixed-order **30-packet** initialization sent with 30ms gaps between each packet. It MUST originate from PC source port 50001.

### 4.1 Phase 1: Pre-config (10 packets)

```
#  Wire hex (header + payload + trailing)
0x0002  32ff0200040000000000010000000000000000000000                 # START_OPS — MUST be first
0x005F  32ff5f000600000000000100000000000000000000000000             # PRE_CONFIG ×3 (handshake)
0x005F  32ff5f000600000000000100000000000000000000000000
0x005F  32ff5f000600000000000100000000000000000000000000
0x001D  32ff1d00040000000000010000000000000000000000                 # SET_PARAM_1D
0x001B  32ff1b00040000000000010000000000000000000000                 # SET_PARAM_1B
0x0005  32ff0500040000000000010000000000000002000000                 # SET_PARAM_5 (value=2)
0x0018  32ff1800040000000000010000000000000000000000                 # HEARTBEAT (initial)
0x0019  32ff19000400000000000100000000000000bb000000                 # SET_PARAM_19 (0xBB)
0x0021  32ff2100040000000000010000000000000001000000                 # SET_PARAM_21 (value=1)
```

### 4.2 Phase 2: Status queries (5 packets)

```
0x005A  32ff5a000000000000000100000000000000                         # STATUS ×5
0x005A  32ff5a000000000000000100000000000000
0x005A  32ff5a000000000000000100000000000000
0x005A  32ff5a000000000000000100000000000000
0x005A  32ff5a000000000000000100000000000000
```

### 4.3 Phase 3: Hardware init (1 packet)

```
0x0001  32ff01003200000000000100000000000000
        32000000320000003200000032000000     ← payload (68 bytes)
        32000000320000003200000000000000
        010001000100000000000000
        c0250000                             ← trailing = 0xC025
```

### 4.4 Phase 4: Frequencies (3 packets, dynamically built)

```python
dds = int((rx_freq + 30500.0) * 10)   # RX DDS includes IF offset
vfo = int(rx_freq * 10)               # TX VFO is exact

# TX_FREQ (0x0009): [index=0, vfo]
# RX_FREQ (0x0008): [index=0, dds]
# RX_FREQ (0x0008): [index=0, dds]  (duplicate)
```

Example for 7.074 MHz:
- vfo = 70,740,000 → `50 37 04 00` (LE)
- dds = 71,045,000 → `8d cb 43 00` (LE)

Wire hex:
```
TX_FREQ: 32ff09000800000000000100000000000000000000005037040000000000
RX_FREQ: 32ff08000800000000000100000000000000000000008dcb430000000000
RX_FREQ: 32ff08000800000000000100000000000000000000008dcb430000000000
```

### 4.5 Phase 5: Post-init + stream start (10 packets)

```
0x0017  32ff17000400000000000100000000000000dc000000                 # SET_PARAM_17 (0xDC)
0x001E  32ff1e00040000000000010000000000000000000000                 # SET_PARAM_1E
0x0015  32ff1500040000000000010000000000000001000000                 # SET_PARAM_15 (value=1)
0x0007  32ff07001a00000000000100000000000000                         # SET_PARAM_7 (26B zeros)
        0000000000000000000000000000000000000000000000000000
0x0024  32ff2400040000000000010000000000000000000000                 # SET_PARAM_24
0x0020  32ff20003400000000000100000000000000                         # ★ STREAM_CTRL ★
        0000000001000000000000000000000064000000
        00000000000000001e000000bc02000007000000
        640000002c01000064000000
0x0018  32ff1800040000000000010000000000000000000000                 # HEARTBEAT
0x0026  32ff2600040000000000010000000000000000000000                 # SET_PARAM_26
0x0027  32ff27001000000000000100000000000000                         # VOX_CTRL
        dc460300b6d20000dc460300b6d20000
0x0022  32ff22000c00000000000100000000000000                         # SET_PARAM_22
        000000000084d71700000000
```

**Key**: `STREAM_CTRL` (0x0020, line 5 of Phase 5) is the command that starts the IQ data flow from the device to the PC on port 50002.

### 4.6 STREAM_CTRL payload decoded (60 bytes)

```
Byte offset | Size | LE value      | Decoded    | Interpretation
0-3         | u32  | 0x00000000    | 0          | RX channel
4-7         | u32  | 0x00000001    | 1          | TX channel
8-11        | u32  | 0x00000000    | 0          |
12-15       | u32  | 0x00000000    | 0          |
16-19       | u32  | 0x00000064    | 100        | Last octet of PC IP (192.168.16.100)?
20-23       | u32  | 0x00000000    | 0          |
24-27       | u32  | 0x00000000    | 0          |
28-31       | u32  | 0x00000000    | 0          |
32-35       | u32  | 0x0000001e    | 30         | Timeout / config flag?
36-39       | u32  | 0x000002bc    | 700        | Buffer / rate param?
40-43       | u32  | 0x00000007    | 7          | Flags?
44-47       | u32  | 0x00000064    | 100        |
48-51       | u32  | 0x0000012c    | 300        | Buffer size?
52-55       | u32  | 0x00000064    | 100        |
```

Trailing word for STREAM_CTRL is `0x64` (100).

---

## 5. Runtime Control Commands

### 5.1 Frequency change

Both RX and TX frequencies must be sent together for the change to take effect.

```python
# RX: DDS = VFO + IF_OFFSET (30500 Hz), unit = 0.1 Hz
await radio.set_rx_frequency(freq_hz)
# → build_packet(0x0008, struct.pack("<II", 0, int((freq_hz + 30500.0) * 10)))

# TX: exact VFO, unit = 0.1 Hz
await radio.set_tx_frequency(freq_hz)
# → build_packet(0x0009, struct.pack("<II", 0, int(freq_hz * 10)))
```

### 5.2 PTT (Push-To-Talk)

```python
# TX ON:
build_packet(0x0006, struct.pack("<I", 0), trailing=1)
# Wire: 32ff060004000000000001000000000000000000000001000000

# TX OFF (RX):
build_packet(0x0006, struct.pack("<I", 0), trailing=0)
# Wire: 32ff060004000000000001000000000000000000000000000000
```

The **trailing word** controls TX/RX state — the payload is always `uint32 0`.

### 5.3 Heartbeat

```python
# Wire: 32ff1800040000000000010000000000000000000000
hb_sock.sendto(bytes.fromhex(
    "32ff1800040000000000010000000000000000000000"),
    ("192.168.16.200", 50001))
```

**Interval**: Every 0.5s. **Timeout**: ~8 minutes without heartbeat → session dies, IQ stream stops.

### 5.4 Client-side only commands (no hardware command sent)

These are maintained as client state but do not generate hardware UDP packets in the DIRECT backend:

`set_mode`, `set_filter`, `set_agc_mode`, `set_volume`, `set_drive`, `set_rf_gain`, `set_preamp`, `set_attenuator`, `set_antenna`, `set_tune`, `set_rit_enable`, `set_rit_offset`, `set_split`, `set_vfo_lock`

Mode, filter, and AGC are handled purely in the software DSP layer (`AudioDemodulator`), not the hardware. The SunSDR2 DX digitizes the entire band as IQ — mode selection is a DSP concept.

---

## 6. IQ Stream Data Flow

Once `STREAM_CTRL` (0x0020) is sent during boot, the device continuously sends IQ data packets to PC port 50002.

### 6.1 IQ packet wire format

```
Byte offset | Size   | Field       | Value
0-1         | uint16 | magic       | 0xFF32
2-3         | uint16 | sub_type    | 0xFFFE (IQ data marker)
4-7         | uint32 | data_len    | varies
8-9         | uint16 | flags       | varies
10..10+N-1  | bytes  | IQ samples  | N = min(200, (data_len-10)/6) × 6 bytes
```

Minimum packet size: 10 (header) + 200×6 = **1210 bytes**. The code requires `len(raw) >= 1200`.

### 6.2 IQ sample decoding (24-bit signed, little-endian interleaved)

Each sample pair = 6 bytes: 3 bytes I + 3 bytes Q, both signed 24-bit LE.

```python
n = min(200, len(payload) // 6)
iq = np.zeros(n, dtype=np.complex64)
for i in range(n):
    off = i * 6
    i_val = int.from_bytes(payload[off:off+3], 'little', signed=True)
    q_val = int.from_bytes(payload[off+3:off+6], 'little', signed=True)
    iq[i] = complex(i_val / 8388608.0, q_val / 8388608.0)
```

- **Normalization divisor**: `8,388,608` = 2^23 (24-bit signed peak = ±8,388,607)
- **Output type**: `numpy.complex64`
- **Samples per packet**: ≤ 200 (exact count depends on packet size)
- **Packet rate**: `78,125 / 200 = 390.625` packets/sec
- **True IQ sample rate**: **78,125 Hz** (5⁷)

### 6.3 Stream keep-alive (PC → Device on port 50002)

The IQ stream requires periodic keep-alive packets sent TO the device on port 50002. Without these, the device may stop streaming.

```python
# RX keep-alive (every 0.5s):
hdr = struct.pack("<HHIH", 0xFF32, 0xFFFE, tx_counter, 0x0001)
iq_sock.sendto(hdr + b'\x00' * 1200, ("192.168.16.200", 50002))

# TX dummy stream (when PTT active, faster):
hdr = struct.pack("<HHIH", 0xFF32, 0xFFFD, tx_counter, 0x0102)
iq_sock.sendto(hdr + b'\x00' * 1200, ("192.168.16.200", 50002))
```

| State | Sub-type | Flags | Interval | Payload |
|-------|----------|-------|----------|---------|
| RX idle | `0xFFFE` | `0x0001` | 0.5s | 1200 zero bytes |
| TX active | `0xFFFD` | `0x0102` | 2.2ms | 1200 zero bytes |

`tx_counter` is a `uint32` that increments by `0x10000` each send, starting from `0x04B0`.

### 6.4 Processing loop (server.py `_process_iq_stream`)

```
while radio.connected:
    ┌─ PTT active?
    │   YES → send 0xFFFD dummy to device:50002, sleep 2.2ms, continue
    │
    ├─ Keep-alive timer ≥ 0.5s?
    │   YES → send 0xFFFE stream keep-alive to device:50002
    │
    ├─ asyncio.wait_for(sock_recvfrom, timeout=0.5)
    │   timeout → (idle stats every 5s) → continue
    │   error   → continue
    │
    ├─ Validate: magic=0xFF32, sub=0xFFFE, len ≥ 1200
    │
    ├─ Decode 24-bit I/Q → complex64
    ├─ dsp_proc.feed_iq(iq)
    │   ├─ SpectrumProcessor.feed() → FFT → latest_spectrum
    │   │   └─ if not None: percentile → getSignalLevel broadcast
    │   │                   _broadcast_spectrum (if clients)
    │   └─ AudioDemodulator.demodulate() → audio buffer
    │       └─ _broadcast_audio (if audio chunks ready)
    │
    └─ Every 5s: log IQ stats (pkt, iq, spec, audio counts)
```

---

## 7. DSP Pipeline

### 7.1 StreamProcessor (`dsp.py`)

```python
@dataclass
class StreamProcessor:
    spectrum: SpectrumProcessor       # FFT accumulation → spectrum
    demodulator: AudioDemodulator     # IQ → audio demodulation
    latest_spectrum: list | None      # 512-bin dB values, consumed by server
    audio_chunks: deque               # maxlen=100, int16 PCM 512-sample chunks
```

`feed_iq(iq)` → routes to both SpectrumProcessor and AudioDemodulator, stores results.

### 7.2 SpectrumProcessor

**FFT accumulation buffer**: 2048 samples of complex64, filled incrementally from 200-sample IQ chunks.

```python
# When buffer fills (2048 samples accumulated):
windowed = buffer * np.hanning(2048)
spec = np.fft.fftshift(np.abs(np.fft.fft(windowed)))
spec_db = np.clip(20 * np.log10(spec + 1e-10), -120, 0)
# → return 2048-point float32 dB spectrum, clipped to [-120, 0] dB
```

**Binning** (reduces to 512 bins for transmission):

```python
# 2048 points → 512 bins by averaging groups of 4
bins = np.mean(spec[:2048].reshape(512, 4), axis=1)
```

**Trigger rate**: 2048 / 200 ≈ 10.24 packets → ~38.1 spectra/sec (78,125/2048).

### 7.3 AudioDemodulator

**Filter design** (`_build_filters`, called at init):

| Filter | Type | Cutoff | Transition | Attenuation | Window |
|--------|------|--------|------------|-------------|--------|
| USB bandpass | Complex FIR | 1500 Hz center | 600 Hz | 60 dB | Hamming |
| LSB bandpass | Complex FIR | 1500 Hz center | 600 Hz | 60 dB | Hamming |
| AM/FM lowpass | Real FIR | 3200 Hz | 800 Hz | 60 dB | Hamming |

USB/LSB filters are built as complex frequency-shifted prototypes:
```python
proto = design_fir_lowpass(cutoff=1500, ...)  # real LPF
wc = 2π × 1500 / 78125
bp_usb = proto × exp(+j × wc × n)   # shift up by 1500 Hz
bp_lsb = proto × exp(-j × wc × n)   # shift down by 1500 Hz
```

Filter state is preserved between calls via `lfilter(zi=...)` for continuous-phase streaming.

**Demodulation chain** (`demodulate`):

```
IQ (complex64, 200 samples @ 78,125 Hz)
  │
  ├─ IF shift: multiply by exp(j × 2π × 30500/78125 × n)
  │   (phase continuity maintained across calls via _lo_phase)
  │   Result: baseband IQ (VFO → 0 Hz)
  │
  ├─ Mode selection:
  │   USB/CW:  lfilter(bp_usb, bb) → real (upper sideband)
  │   LSB:     lfilter(bp_lsb, bb) → real (lower sideband)
  │   AM:      |bb| → lfilter(lpf) → dc-remove
  │   FM/NFM:  angle(bb) → unwrap → diff → lfilter(lpf)
  │
  ├─ Decimate: audio[::5]  → 15,625 Hz
  │
  ├─ Built-in AGC:
  │   rms = sqrt(mean(audio²))
  │   target = 0.25 × volume × 2.0
  │   gain = gain × 0.95 + (target / rms) × 0.05   (smoothing)
  │   audio ×= gain; clip to [-1, 1]
  │
  ├─ WDSP post-processing (if enabled + libwdsp available):
  │   audio = _wdsp.process(audio)   → NR2, NB, ANF, AGC
  │
  └─ Return float32 audio
```

**Audio buffering**:

```python
# In StreamProcessor.feed_iq():
self.demodulator.audio_buffer.extend(audio.tolist())

# When buffer ≥ 512 samples:
chunk = array of 512 float32s from buffer
int16_bytes = clip(chunk × 32767, -32768, 32767).astype(int16).tobytes()
self.audio_chunks.append(int16_bytes)
```

### 7.4 Filter reconfigure (runtime)

When user changes filter bandwidth:

```python
def reconfigure_filter(low_hz=200, high_hz=3000):
    cutoff = max(min(high_hz, 4000), 500)
    proto = design_fir_lowpass(cutoff=cutoff, sample_rate=78125,
                                attenuation_db=60, transition_width_hz=min(600, cutoff//2))
    wc = 2π × (cutoff/2) / 78125
    bp_usb = proto × exp(+j × wc × n)   # new USB filter
    bp_lsb = proto × exp(-j × wc × n)   # new LSB filter
    # Reset filter state buffers
```

---

## 8. WDSP Integration

WDSP = Warren Pratt DSP library (`libwdsp.dylib`, ARM64). Provides hardware-quality AGC, spectral noise reduction (NR2), noise blanker (NB), and auto notch filter (ANF).

### 8.1 Library loading

Searches in order:
1. `/usr/local/lib/libwdsp.dylib`
2. `/opt/homebrew/lib/libwdsp.dylib`
3. `web_control/libwdsp.dylib` (bundled copy)

Fallback: if no library found, WDSP is disabled and all setters are safe no-ops.

### 8.2 WDSPMode enum

```
LSB=0, USB=1, DSB=2, CW=3, AM=4, FM=5
```

### 8.3 WDSPAGCMode enum

```
OFF=0, LONG=1, SLOW=2, MED=3, FAST=4
```

### 8.4 WDSPProcessor initialization

```python
WDSPProcessor(
    sample_rate=15625,      # matches demodulator audio rate
    buffer_size=256,        # processing block size
    mode=WDSPMode.USB,
    enable_nr2=True,        # spectral noise reduction ON by default
    enable_nb=False,        # noise blanker OFF by default
    enable_anf=False,       # auto notch OFF by default
    agc_mode=WDSPAGCMode.SLOW)
```

C library initialization sequence:
```
OpenChannel(ch=0, in_size=256, out_size=256, in_rate=15625, out_rate=15625, ...)
SetRXAMode(ch=0, mode)
SetRXAPanelGain1(ch=0, 0.06)
SetRXAAGCMode(ch=0, agc_mode)
SetRXAEMNRRun(ch=0, 1)           # NR2 enable
SetRXAEMNRgainMethod(ch=0, 0)    # gain method = linear
SetRXAEMNRnpeMethod(ch=0, 0)     # NPE method = standard
SetRXAEMNRaeRun(ch=0, 1)         # acquisition enhancement ON
SetRXAEMNRPosition(ch=0, 0)      # NR2 position in chain
SetRXASNBARun(ch=0, 1)           # noise blanker ON
```

### 8.5 Processing

```python
def process(audio_data):
    # Input: float32/int16 → padded/truncated to 256 samples → float64
    # Interleave: _in[0::2] = audio, _in[1::2] = 0.0
    # Call: fexchange0(ch, _in, _out, &err)
    # err=0: success, err=-2: warmup, other: error (fallback to pass-through)
    # Output: _out[0::2] → float32, truncated to original length
```

### 8.6 Runtime control methods

| Method | C function called | Notes |
|--------|------------------|-------|
| `set_agc(mode)` | `SetRXAAGCMode` | 0-4 |
| `set_mode(mode)` | `SetRXAMode` | 0-5 |
| `set_nr2_enabled(on)` | `SetRXAEMNRRun` | Toggle NR2 |
| `set_nr2_level(level)` | `SetRXAEMNRgainMethod` + `SetRXAEMNRnpeMethod` + `SetRXAEMNRaeRun` | Level 0-100 |
| `set_nr2_gain_method(m)` | `SetRXAEMNRgainMethod` | 0=linear, 1=log |
| `set_nr2_npe_method(m)` | `SetRXAEMNRnpeMethod` | 0=standard |
| `set_nr2_ae_run(on)` | `SetRXAEMNRaeRun` | Acquisition enhancement |
| `set_nb_enabled(on)` | `SetRXASNBARun` | Noise blanker |
| `set_anf_enabled(on)` | `SetRXAANFRun` | Auto notch filter |
| `set_nf_enabled(on, freq)` | `SetRXAMANFRun` + `SetRXAMNFreq` | Manual notch (may not be in all builds) |
| `set_bandpass(lo, hi)` | `SetRXABandpassFilter` | Audio bandpass (may not be in all builds) |
| `close()` | `SetChannelState` + `CloseChannel` | Clean shutdown |

### 8.7 AudioDemodulator state tracking

All WDSP state is mirrored in the demodulator for front-end queries:

```python
{
    "enabled": bool,       # WDSP master toggle
    "nr2": bool,           # NR2 enabled
    "nr2Level": int,       # 0-100
    "nb": bool,            # Noise blanker enabled
    "anf": bool,           # Auto notch filter enabled
    "nf": bool,            # Manual notch filter enabled
    "agcMode": int,        # 0=OFF, 1=LONG, 2=SLOW, 3=MED, 4=FAST
    "notches": [           # Manual notch list
        {"id": int, "freq": float, "width": float}
    ],
    "available": bool      # libwdsp.dylib was loaded
}
```

---

## 9. WebSocket Protocol

### 9.1 Endpoint summary

| Endpoint | Type | Direction | Payload | Rate |
|----------|------|-----------|---------|------|
| `/WSCTRX` | Text | Bidirectional | `cmd:val` or `cmd` (PING/PONG) | On demand |
| `/WSaudioRX` | Binary | Server → Client | Int16 PCM, 16 kHz | ~32 KB/s |
| `/WSaudioTX` | Binary | Client → Server | Not consumed (placeholder) | — |
| `/WSspectrum` | Binary | Server → Client | 512 uint8, 0=-120dB, 255=0dB | ~19 KB/s @ 38 Hz |
| `/WSATR1000` | Text (JSON) | Bidirectional | `{"action":"sync"}` etc. | 2 Hz (heartbeat) |

### 9.2 WSCTRX — Control commands

Format: `command:value` (colon-separated). Special commands without colon: `PING`.

#### Liveness
| Command | Response | Notes |
|---------|----------|-------|
| `PING` | `PONG` | Latency measurement. Must be handled before colon check. |

#### Query commands (Server → Client response)
| Command | Response format | Source |
|---------|----------------|--------|
| `getFreq` | `getFreq:7074000` | `radio.rx_freq` |
| `getMode` | `getMode:USB` | `dsp_proc.demodulator.mode` |
| `getPTT` | `getPTT:false` | `radio.ptt` |
| `getWDSPStatus` | `wdspStatus:{...json...}` | `demodulator.get_wdsp_status()` |
| `getWDSPNotches` | (same as getWDSPStatus) | Same |

#### Radio commands
| Command | Example | Target |
|---------|---------|--------|
| `setFreq` | `setFreq:7074000` | `radio.set_frequency()` → RX_FREQ + TX_FREQ |
| `setMode` | `setMode:LSB` | `demodulator.set_mode()` (DSP only) |
| `setPTT` | `setPTT:true` | `radio.set_ptt()` + `demodulator.set_ptt()` |
| `tune` | `tune:true` | `radio.set_tune()` (no-op in DIRECT) |
| `setAFGain` | `setAFGain:50` | `demodulator.set_volume(val/100)` + `radio.set_volume()` |
| `setRFGain` | `setRFGain:80` | `radio.set_rf_gain(val/100)` |
| `setPreamp` | `setPreamp:true` | `radio.set_preamp()` |
| `setAGC` | `setAGC:AUTO` | `radio.set_agc_mode()` |
| `setFilter` | `setFilter:200,2800` | `demodulator.reconfigure_filter()` + `radio.set_filter()` |

#### WDSP commands (all DSP-side, broadcast to all clients)
| Command | Example | Method called |
|---------|---------|---------------|
| `setWDSPEnabled` | `setWDSPEnabled:true` | `demodulator.set_wdsp_enabled()` |
| `setWDSPNR2Level` | `setWDSPNR2Level:75` | `demodulator.set_nr2_level()` |
| `setWDSPNR2` | `setWDSPNR2:true` | `demodulator.set_nr2_enabled()` |
| `setWDSPNB` | `setWDSPNB:false` | `demodulator.set_nb_enabled()` |
| `setWDSPANF` | `setWDSPANF:true` | `demodulator.set_anf_enabled()` |
| `setWDSPNFEnabled` | `setWDSPNFEnabled:false` | `demodulator.set_nf_enabled()` |
| `setWDSPNR2GainMethod` | `setWDSPNR2GainMethod:1` | `demodulator.set_nr2_gain_method()` |
| `setWDSPNR2NpeMethod` | `setWDSPNR2NpeMethod:0` | `demodulator.set_nr2_npe_method()` |
| `setWDSPNR2AeRun` | `setWDSPNR2AeRun:true` | `demodulator.set_nr2_ae_run()` |
| `setWDSPBandpass` | `setWDSPBandpass:200,3000` | `demodulator.set_bandpass()` |
| `setWDSPAGC` | `setWDSPAGC:3` | `demodulator.set_agc_mode()` |
| `addWDSPNotch` | `addWDSPNotch:1500,100` | `demodulator.add_notch()` |
| `editWDSPNotch` | `editWDSPNotch:0,1600,120` | `demodulator.edit_notch()` |
| `deleteWDSPNotch` | `deleteWDSPNotch:0` | `demodulator.delete_notch()` |

#### Safety commands
| Command | Action |
|---------|--------|
| `s` | Force RX: `radio.set_ptt(False)`, `demodulator.set_ptt(False)`, broadcast `getPTT:false` |
| `cq` | CQ complete acknowledgement: broadcast `cq:complete` |

### 9.3 Connection lifecycle

```
Client connects → accepted → added to client set
Client sends messages (ignored for RX-only endpoints)
Client disconnects → WebSocketDisconnect or RuntimeError → removed from set
```

All 5 endpoints use fan-out pattern: server maintains `set[WebSocket]`, iterates to broadcast, removes dead connections on send failure.

---

## 10. S-Meter & Spectrum Broadcasting

### 10.1 S-meter calculation

```python
# In _process_iq_stream, when latest_spectrum is available:
spec = dsp_proc.latest_spectrum   # 512 float dB values in [-120, 0]
p90 = float(np.percentile(spec, 90))
s9 = max(0, min(60, int(9 + (p90 + 73) / 6)))
# Broadcast: "getSignalLevel:<s9>"
```

Formula: `S = clamp(0, 60, 9 + (P90_dB + 73) / 6)`  
Where P90 is the 90th percentile of the 512-bin dB spectrum.  
At 0 dB (max signal): S = 9 + 73/6 ≈ 21  (S9+12)  
At -120 dB (noise floor): S = 9 + (-47)/6 ≈ 1

### 10.2 Spectrum broadcast format

```python
def _broadcast_spectrum(spec):
    # spec: list of ~512 float32 dB values in [-120, 0]
    arr = np.asarray(spec, dtype=np.float32)
    if arr.size == 0: return
    # Quantize: 0 = -120 dB (black), 255 = 0 dB (bright)
    q = np.clip((arr + 120.0) * (255.0 / 120.0), 0, 255).astype(np.uint8)
    frame = q.tobytes()   # 512 bytes
    # Fan-out to all spectrum_clients
    for ws in spectrum_clients:
        await ws.send_bytes(frame)
```

**Rate**: ~38 frames/sec × 512 bytes = **~19 KB/s** per connected spectrum client.

### 10.3 Frontend waterfall rendering (controls.js)

```
WebSocket binary message → Uint8Array of 512 bins
  ↓
Accumulate WF_DECIMATE=10 frames (Float32Array sum)
  ↓ ~3.8 Hz (38 Hz / 10)
Adaptive noise floor: sort bins, take WF_PCTL=30th percentile
  ↓
floor = percentile + WF_HEADROOM=2
  ↓
For each bin: value = WF_BIAS(52) + (accumulated - floor) × WF_GAIN(8.0)
  ↓
Clamp 0-255, map through color LUT:
  - 0-63:    black → deep blue   (noise floor)
  - 64-127:  deep blue → cyan    (weak signals)
  - 128-191: cyan → yellow       (moderate signals)
  - 192-255: yellow → red        (strong signals)
  ↓
Draw 1 row at top of canvas, shift rest down
```

Color ramp formula:
```javascript
if (t < 0.25):      r=0, g=0, b=40+u*160          // black→deep blue
else if (t < 0.5):  r=0, g=u*200, b=200+u*55      // deep blue→cyan
else if (t < 0.75): r=u*255, g=200+u*55, b=255*(1-u)  // cyan→yellow
else:               r=255, g=255*(1-u), b=0        // yellow→red
```

---

## 11. Audio Broadcasting

### 11.1 Server-side resampling

Audio is generated at 15,625 Hz (native demodulator rate) and resampled to 16,000 Hz for browser compatibility.

```python
def _broadcast_audio(pcm: bytes):
    # pcm: 512 samples of int16 LE @ 15,625 Hz
    arr = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
    if len(arr) < 16: return

    # Resample: 15,625 → 16,000 Hz using linear interpolation
    out_len = int(len(arr) * 16000 / 15625)
    out = np.interp(
        np.linspace(0, len(arr)-1, out_len),   # target positions
        np.arange(len(arr)),                     # source positions
        arr                                      # source values
    ).astype(np.int16)

    frame = out.tobytes()
    for ws in audio_rx_clients:
        await ws.send_bytes(frame)
```

**Resample ratio**: 16,000 / 15,625 = 1.024 → each 512-sample chunk becomes ~524 samples.  
**Bitrate**: 524 samples × 2 bytes × 38.1 chunks/sec ≈ **40 KB/s** per audio client.

### 11.2 Audio chunk generation timing

```
IQ packets (200 samples/pkt, 390.6 pkt/s)
  ↓ demodulate → ~40 audio samples per packet (200/5)
  ↓ accumulate in audio_buffer (deque, maxlen=31250)
  ↓ every 512 accumulated → extract chunk → audio_chunks (deque, maxlen=100)
  ↓ get_audio() → pops from audio_chunks
  ↓ _broadcast_audio() → resample → send to clients
```

Chunk frequency: 15,625 / 512 ≈ **30.5 chunks/sec**.

---

## 12. ATR-1000 Protocol

### 12.1 Frontend → Server messages

| Action | JSON | When |
|--------|------|------|
| `sync` | `{"action":"sync"}` | Every 2s heartbeat (power on) |
| `start` | `{"action":"start"}` | TX begins / PTT press |
| `stop` | `{"action":"stop"}` | TX ends / PTT release / disconnect |

### 12.2 Server behavior

Currently a placeholder — accepts connections and JSON messages, suppresses sync logging (to avoid log flood), echoes nothing back. Full tuner HW integration is planned but not implemented.

### 12.3 Frontend state machine

```
Power ON → connect /WSATR1000 → send sync every 2s
PTT press → send start → show tuner panel
PTT release → send stop → hide tuner panel
Power OFF → send stop → close WebSocket
```

---

## 13. Frontend Data Processing

### 13.1 S-meter smoothing (mobile.js)

```javascript
// Asymmetric exponential smoothing:
// S_ATTACK = 0.5   (fast rise)
// S_RELEASE = 0.15 (slow fall)
if (newLevel >= currentLevel) {
    displayLevel = currentLevel + (newLevel - currentLevel) * S_ATTACK;
} else {
    displayLevel = currentLevel + (newLevel - currentLevel) * S_RELEASE;
}
```

### 13.2 Audio playback (controls.js)

```
WSaudioRX binary message → Int16Array
  ↓
Convert to Float32 (÷32768)
  ↓
AudioWorklet (rx_worklet_processor.js)
  or ScriptProcessorNode fallback
  ↓
AudioContext.destination → speakers/headphones
```

Format: 16 kHz mono Int16 PCM. The server handles all resampling from 15,625 Hz native rate.

### 13.3 PTT safety flow (AD-007)

```
PTT press:
  send setPTT:true → wait for getPTT:true ACK
  → if no ACK within timeout, retry
  → watchdog timer monitors connection

PTT release:
  send setPTT:false → wait for getPTT:false ACK
  → if no ACK within timeout, retry
  → 's' backup command on WSaudioTX channel (server forces RX)
  → watchdog: if no ACK after retries, force TX off locally

Server backup:
  's' command on WSCTRX unconditionally calls radio.set_ptt(False)
  → broadcast getPTT:false to all clients
```

---

## 14. Full Lifecycle

```
┌─ STARTUP ────────────────────────────────────────────────────────┐
│                                                                   │
│  PC: bind 192.168.16.100:50001 (control socket)                   │
│  PC: bind 192.168.16.100:50002 (IQ receive socket)                │
│                                                                   │
│  ┌─ BOOT (30 packets, 30ms spacing) ──────────────────────────┐  │
│  │ Phase 1: Pre-config (10 pkts)                              │  │
│  │   0x0002 START_OPS → 0x005F ×3 → 0x001D → 0x001B →        │  │
│  │   0x0005 → 0x0018 HEARTBEAT → 0x0019 → 0x0021             │  │
│  │ Phase 2: Status queries (5 pkts)                           │  │
│  │   0x005A STATUS ×5                                         │  │
│  │ Phase 3: HW init (1 pkt)                                   │  │
│  │   0x0001 HW_INIT (68B payload)                             │  │
│  │ Phase 4: Frequencies (3 pkts, dynamic)                     │  │
│  │   0x0009 TX_FREQ + 0x0008 RX_FREQ ×2                       │  │
│  │ Phase 5: Stream start (10 pkts)                            │  │
│  │   0x0017 → 0x001E → 0x0015 → 0x0007 → 0x0024 →           │  │
│  │   ★ 0x0020 STREAM_CTRL → 0x0018 → 0x0026 → 0x0027 →     │  │
│  │   0x0022                                                   │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  Start DSP: StreamProcessor(SpectrumProcessor, AudioDemodulator)  │
│  Start tasks: _heartbeat_task(), _process_iq_stream()             │
└───────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ STEADY STATE ────────────────────────────────────────────────────┐
│                                                                   │
│  ┌─ HEARTBEAT (every 0.5s) ────────────────────────────────────┐ │
│  │ 0x0018 to device:50001 (keeps session alive)                │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─ STREAM KEEP-ALIVE (every 0.5s) ────────────────────────────┐ │
│  │ 0xFFFE to device:50002 (keeps IQ stream alive)              │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─ IQ RECEIVE (continuously) ─────────────────────────────────┐ │
│  │ ← device:50002 → 24-bit I/Q decode → feed_iq() → DSP chain │ │
│  │ → spectrum (38 Hz) + audio (30.5 chunks/sec)                │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─ WEBSOCKET BROADCAST ───────────────────────────────────────┐ │
│  │ → /WSspectrum: 512-byte uint8 rows (38 Hz, ~19 KB/s/client) │ │
│  │ → /WSaudioRX: Int16 PCM frames (30.5 Hz, ~40 KB/s/client)   │ │
│  │ → /WSCTRX: getSignalLevel:N (text, ~38 Hz)                  │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─ ATR-1000 HEARTBEAT (every 2s) ─────────────────────────────┐ │
│  │ ← Client → /WSATR1000: {"action":"sync"}                     │ │
│  └─────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ TX STATE (PTT active) ───────────────────────────────────────────┐
│  0x0006 (trailing=1) → device (TX enable)                        │
│  Stream keep-alive → 0xFFFD at 2.2ms intervals                   │
│  Audio demodulator: clear buffer, reset AGC gain                  │
│  IQ receive: continue (monitor TX via sidetone/spectrum)         │
└───────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ SHUTDOWN ────────────────────────────────────────────────────────┐
│  0x0006 (trailing=0) → force RX                                  │
│  Close WDSP channel: SetChannelState + CloseChannel               │
│  Close sockets: 50001, 50002                                      │
└───────────────────────────────────────────────────────────────────┘
```

---

## 15. Data Rate Summary

| Stream | Direction | Rate | Format | Bandwidth |
|--------|-----------|------|--------|-----------|
| IQ samples (raw) | Device → PC | 78,125 Hz | 24-bit I+Q complex | 468.75 KB/s |
| IQ packets | Device → PC | 390.6 pkt/s | UDP, 200 smp/pkt | ~475 KB/s |
| Heartbeat | PC → Device | 2 Hz (0.5s) | 18-byte UDP | 36 B/s |
| Stream keep-alive | PC → Device | 2 Hz (0.5s) | 1218-byte UDP | 2.4 KB/s |
| TX dummy stream | PC → Device | 455 Hz (2.2ms) | 1218-byte UDP | 554 KB/s |
| Spectrum WS | Server → Browser | 38.1 Hz | 512 B uint8 | 19.5 KB/s/client |
| Audio WS | Server → Browser | 30.5 chunk/s | ~524 B int16 | 40 KB/s/client |
| Control WS | Bidirectional | On demand | Text | Negligible |
| ATR heartbeat | Browser → Server | 0.5 Hz (2s) | ~20 B JSON | Negligible |

**Total server → browser** (1 client): ~60 KB/s  
**Total server → browser** (2 clients): ~119 KB/s

---

## 16. Hex Reference

### 16.1 Boot sequence (all 30 packets)

```
Phase 1 — Pre-config:
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

Phase 2 — Status queries:
32ff5a000000000000000100000000000000
32ff5a000000000000000100000000000000
32ff5a000000000000000100000000000000
32ff5a000000000000000100000000000000
32ff5a000000000000000100000000000000

Phase 3 — HW init:
32ff01003200000000000100000000000000320000003200000032000000320000003200000032000000320000003200000000000000010001000100000000000000c0250000

Phase 5 — Stream start:
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

### 16.2 Runtime commands

```
Heartbeat:       32ff1800040000000000010000000000000000000000
PTT ON (TX):     32ff060004000000000001000000000000000000000001000000
PTT OFF (RX):    32ff060004000000000001000000000000000000000000000000
```

### 16.3 Frequency command (example: 7.074 MHz)

```
VFO = 7074000 Hz, DDS = 7104500 Hz (×10 = 70740000, 71045000)

TX_FREQ: 32ff09000800000000000100000000000000000000005037040000000000
         └─ 0x0009 ─┘└─ len=8──┘└─ index ─┘└─ reserved ─┘└─ idx=0 ─┘└─ 70740000 LE ─┘└─ trail=0 ─┘

RX_FREQ: 32ff08000800000000000100000000000000000000008dcb430000000000
         └─ 0x0008 ─┘└─ len=8──┘└─ index ─┘└─ reserved ─┘└─ idx=0 ─┘└─ 71045000 LE ─┘└─ trail=0 ─┘
```

### 16.4 Stream keep-alive (to device port 50002)

```
RX: 32fffe00b00400010000000000000000 + 1200 zero bytes
    └─ 0xFFFE ─┘└─ len=1200 ─┘└─ flags=0x0001 ─┘

TX: 32fffd00b00400020001000000000000 + 1200 zero bytes
    └─ 0xFFFD ─┘└─ len=1200 ─┘└─ flags=0x0102 ─┘
```

### 16.5 IQ packet (from device port 50002)

```
32fffe00XXXXXXXXXXXX...    (10-byte header + 200×6=1200 bytes IQ)
└─ 0xFFFE ─┘└─ len ─┘└─ flags ─┘└─ 24-bit I/Q samples (200 pairs) ─┘
```

### 16.6 Packet anatomy (generic)

```
Offset:  0  1  2  3  4  5  6  7  8  9 10 11 12 13  14..14+N-1  14+N..14+N+3
Hex:    32 ff XX XX YY YY YY YY 00 00 01 00 00 00  [payload...]  [trailing...]
Field:  magic─┘ cmd─┘ data_len──┘ index──────┘ res─┘ payload───┘ trailing───┘
```

---

## 17. TX Chain — Verified from Capture

> Source: `device/captures/sunsdr_sdr_tx.pcap` (genuine ExpertSDR3 1.0.17 transmit, 3 PTT
> bursts). Analysis scripts and machine-readable results in `device/`. These findings
> **correct and extend** the earlier sections, which were reverse-engineered before a TX
> capture was available.

### 17.1 What flows during transmit

When PTT is engaged, the PC streams two packet types to **device port 50002**, interleaved
roughly **8× `0xFFFD` per 1× `0xFFFE`**:

| Sub-type | Flags | Role | Payload |
|----------|-------|------|---------|
| `0xFFFD` | `0x0102` | **TX IQ data** — the modulated signal to transmit | 200 samples × 6 bytes (24-bit I/Q) |
| `0xFFFE` | `0x0001` | RX stream keep-alive (continues during TX) | 1200 zero bytes |

Observed wire counts in the reference: 1106 × `0xFFFD`, 1360 × `0xFFFE`.

PTT itself is the `0x0006` control packet on port 50001 (trailing `1`=TX / `0`=RX), exactly
as documented in §5.2 — confirmed by 6 PTT events in the capture (3 on/off pairs).

### 17.2 TX IQ packet format (16-byte header + 1200-byte payload)

```
Byte offset | Size   | Field    | Value
0-1         | uint16 | magic    | 0xFF32
2-3         | uint16 | sub_type | 0xFFFD
4-7         | uint32 | counter  | starts 0x04B0, += 0x10000 per packet
8-9         | uint16 | flags    | 0x0102
10..1209    | bytes  | IQ       | 200 × (3B I + 3B Q), signed 24-bit LE
```

Example header (wire): `32 ff fd ff b0 04 13 00 02 01`

> **Doc correction:** §16.5 and the generic anatomy (§16.6) label bytes 4-7 as `data_len`.
> That is true for **control** packets on 50001, but in **stream** packets on 50002 (both
> `0xFFFD` and `0xFFFE`) bytes 4-7 are a **packet counter**. The counter is shared between
> the two sub-types and **resets to `0x04B0` at each PTT press**.

### 17.3 TX sample rate = 39,063 Hz (RX/2) — corrected

Measured median inter-frame interval is **5.12 ms** across all three bursts (195.8 pkt/s ×
200 samples):

| Burst | Packets | Duration | Rate | Implied SR |
|-------|---------|----------|------|-----------|
| 1 | 324 | 1.653 s | 196.0 pkt/s | 39.2 kHz |
| 2 | 391 | 1.997 s | 195.8 pkt/s | 39.2 kHz |
| 3 | 391 | 1.997 s | 195.8 pkt/s | 39.2 kHz |

**TX IQ runs at 39,063 Hz (5⁷ / 2)** — the *lowest* of the manual's `39; 78; 156; 312 kHz`
rate options. RX in the same capture runs at the full **78,125 Hz** (2.554 ms/pkt). TX is
therefore **half** the RX rate.

> **Doc/code correction:** `server.py` uses `TX_INTERVAL = 200/78125 = 2.56 ms`, which is
> **2× too fast**. The device consumes TX IQ at 5.12 ms/pkt. Overfeeding overruns the
> hardware TX buffer and is a likely source of the periodic popping noted in `TXPLAN.md`.
> ExpertSDR3 sends in tight bursts (sub-ms gaps) but the *average* rate holds at 195.8 pkt/s.

### 17.4 TX IQ level — peak ≈ 0.09, NOT 0.3

The genuine ExpertSDR3 TX IQ never exceeds **|IQ| ≈ 0.092** (24-bit normalized), with
per-packet RMS in the **0.002–0.043** range:

```
global peak |IQ| = 0.09189
per-packet RMS   = 0.002 … 0.043 (mean 0.024 in the voice burst)
```

> **Code correction:** `dsp.py` normalizes modulation to `0.3` (`_modulate`,
> `generate_test_tone`, `set_tune_wav`). This is **~3.3× hotter** than the reference and,
> with the manual confirming **ALC is not yet supported in firmware**, there is no hardware
> limiter to catch it — the over-level IQ clips in the PA. Target a **~0.09 peak** and scale
> below that with a drive control. This is the single largest divergence between the code
> and the capture.

### 17.5 Leading silence before audio (PA settling)

Each burst begins with **~17 all-zero IQ packets (~87 ms @ 5.12 ms/pkt)** before any audio
energy appears. This matches the manual (§ "开始发射前…使用单独的EXT_CTRL控制该继电器…可调
PTT切换延迟"): the PA / antenna relay needs settling time. Implementation should send a short
run of zero-IQ packets immediately after asserting PTT, before feeding real modulation.

### 17.6 Device → PC TX telemetry (newly observed)

During TX the device sends two small telemetry sub-types back on 50002 (836 + 7 frames in
the reference):

| Sub-type | Size | Content |
|----------|------|---------|
| `0x1F00` | 34 B | Floats (e.g. `45.0`, `1.0`) — likely PA temp / forward power / SWR |
| `0x1F01` | 22 B | Mostly zeros — TX status flags |

These are not yet decoded or consumed by the server. They are the probable source for a real
TX power/SWR meter. Captured for future analysis in `device/`.

### 17.7 Corrected summary vs. current code

| Item | Code (`dsp.py`/`server.py`) | Capture (verified) | Action |
|------|------------------------------|--------------------|--------|
| TX IQ peak level | `0.3` | `~0.09` | Lower to ~0.09 + drive scaling |
| TX packet interval | `2.56 ms` (200/78125) | `5.12 ms` (200/39063) | Halve the rate |
| TX sample rate | implied 78,125 Hz | **39,063 Hz** | Modulate at 39 kHz |
| Pre-TX silence | none | ~17 zero packets | Send settling pad after PTT |
| Stream byte 4-7 | n/a | packet counter (resets per PTT) | (doc) |

### 17.8 TX chain — as implemented

The voice/tune TX path is now wired end-to-end. Summary of the implementation
(`web_control/dsp.py`, `sunmrrc/server.py`, `sunmrrc/static/`):

**Packet counter (race fixed).** `tx_counter` is shared between the 0xFFFD pacer
thread and the 0xFFFE keep-alive sender. A `threading.Lock` (`_next_tx_counter()`)
now guards every `+= 0x10000`, keeping the on-wire counter strictly monotonic as
the capture requires (a lost non-atomic `+=` previously produced duplicate/
non-monotonic counters → device packet drop → periodic clicking).

**TX IQ level.** `TX_IQ_PEAK` is the single normalization ceiling for all paths
(voice, tune, test tone). Set to `0.4` for bench testing — the capture reference
is `~0.09`, so this is ~4.4× hotter; with firmware ALC unsupported it can clip in
the PA, so recalibrate against measured output power. `drive` (0..1) scales below
this ceiling.

**Start ramp.** `TX_RAMP_SAMPLES` (~1 packet) applies a linear 0→1 amplitude ramp
to the first IQ after PTT, removing the hard step from the zero-IQ settling pad
to full-amplitude modulation (a click source). Re-armed via `reset_tx_ramp()` on
each PTT assert.

**Mic uplink (PCM, not Opus).** The frontend `#encode` checkbox now defaults
**unchecked** → mic frames are sent as raw **Int16 PCM** (16 kHz mono, 320
samples/frame), matching the backend (AD-004; Opus was removed). With `encode`
checked the browser sent compressed Opus bytes that the backend decoded as PCM →
garbage audio. `/WSaudioTX` feeds binary frames to `TXModulator.feed_audio()`.

**Mic → IQ pipeline** (`feed_audio`, all phase-continuous across bursty 20 ms WS
frames):
1. Continuous fractional resampler `input_rate → 15625 Hz` with a persistent
   input buffer + fractional read cursor (no per-frame reset → no seam buzz).
2. Overlap-save Hilbert SSB in 80-sample (5.12 ms) hops, keeping
   `TX_HILBERT_MARGIN` (256) samples of context each side so block edges stay
   clean. `USB = hilbert`, `LSB = conj(hilbert)`, `AM = envelope`.
3. Fixed-gain scale `TX_IQ_PEAK × drive` (no per-chunk peak normalization, so
   voice dynamics survive) → 200 IQ samples → one 0xFFFD packet.

**Jitter buffer.** `get_mic_iq()` stays un-primed (emits silence) until
`TX_MIC_PRIME_PKTS` (12) packets accumulate, then drains one per pacer tick. On
underflow it re-primes, so a momentary WS stall yields continuous silence rather
than a mid-stream gap (click). The bursty 20 ms producer vs. 5.12 ms consumer
mismatch was the primary clicking cause.

**TX priority.** `get_tx_iq()`: tune WAV → live mic → silence. There is
intentionally **no 700 Hz test-tone fallback** — PTT with no queued audio emits
silence, not a carrier.

**Tune sideband.** `set_tune_wav()` honors mode (`LSB = conj`); `set_mode()`
recomputes the cached tune IQ when a WAV is loaded so a USB→LSB switch takes
effect on the next playback.

---

## References

- Implementation: `web_control/sunsdr_direct.py` (UDP client), `web_control/dsp.py` (DSP)
- Server: `sunmrrc/server.py` (FastAPI WebSocket bridge)
- WDSP wrapper: `web_control/wdsp_wrapper.py` (libwdsp ctypes)
- Filters: `web_control/gr4_filters.py` (GNU Radio 4.0 FIR design)
- Architecture: `SDD/08-architecture-decisions.md`, `SDD/09-architecture-overview.md`
- Frontend: `sunmrrc/static/controls.js`, `sunmrrc/static/mobile.js`
- **TX capture & analysis: `device/` (pcaps, scripts, `data/tx_analysis.json`)**

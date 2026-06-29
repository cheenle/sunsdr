# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**sunsdr** is a SunSDR2 DX web-based mobile radio control server. It provides HTTPS/WSS access to the radio via a browser, including real-time spectrum waterfall, RX audio streaming, control commands, and PTT management. The frontend targets iPhone/iOS Safari (secure context required for `getUserMedia`).

## Start & restart

```bash
# Restart (kills old process by cwd, frees port, starts in background)
cd sunmrrc && ./restart.sh

# Custom port
WEB_PORT=8889 ./restart.sh

# Foreground (Ctrl-C to stop, live logs)
./restart.sh -f

# HTTP debug mode (no TLS — iOS features will not work)
DISABLE_SSL=1 ./restart.sh

# Check logs
tail -f sunmrrc/server.log
```

Default: `https://localhost:8889` (or `WEB_PORT` env).

## Authentication

All routes (HTTP and WebSocket) require a session token. Unauthenticated visitors are redirected to `/login`.

- **Default password**: `sunmrrc`
- **Custom password**: `WEB_PASSWORD=MySecret123 ./restart.sh`
- **Mechanism**: login page → POST `/api/auth/login` → sets `sunmrrc_auth` cookie → JS reads cookie → passes token as `?token=` query param on all WebSocket connections
- **Token lifetime**: 30 days (cookie max-age); all tokens invalidated on server restart
- **WebSocket auth**: each WS endpoint checks `?token=` query param against `_auth_tokens` server-side set. Browser WebSocket API doesn't support custom headers, hence query-param auth.

## Architecture

See `SDD/09-architecture-overview.md` and `SDD/11-component-model.md` for detailed diagrams. SDD V3.4 (2026-06-26) baseline.

### Logical flow

```
Browser (iOS Safari)
  → HTTPS/WSS (COOP/COEP credentialless for SharedArrayBuffer)
  → FastAPI SunMRRC (sunmrrc/server.py)
    → SunSDR2DXClient (web_control/sunsdr_direct.py) — UDP control port 50001
    → StreamProcessor (web_control/dsp.py) — IQ → spectrum + audio
    → UDP IQ socket port 50002 (pre-bound before boot sequence, dedicated keep-alive thread)
    → SunSDR2 DX hardware @ 192.168.16.200
```

**TX audio path (SharedArrayBuffer ring buffer):**

```
Mic → AudioWorklet (tx_capture_worklet.js) → float32 → SAB ring buffer
  → Opus Worker (tx_opus_worker.js) polls SAB every 3ms, reads 320-sample frames
  → Opus-encodes → dedicated WebSocket → /WSaudioTX
  → server feed_audio() → 300Hz 4th-order Butterworth HPF → Hilbert overlap-save SSB → TX IQ
```

Zero main-thread involvement after setup. COEP `credentialless` (not `require-corp`) avoids breaking Worker `importScripts()` on Safari.

### Key modules

| Module | File | Role |
|--------|------|------|
| Server + WebSockets | `sunmrrc/server.py` | FastAPI app, lifespan, 5 WS endpoints, IQ processing loop, audio/spectrum broadcast |
| Radio client | `web_control/sunsdr_direct.py` | SunSDR2 DX UDP protocol — boot sequence, heartbeat, freq/PTT/AGC setters |
| DSP pipeline | `web_control/dsp.py` | `StreamProcessor` → `SpectrumProcessor` (FFT) + `AudioDemodulator` (SSB/AM/FM + WDSP RX-only). TX: Python Hilbert overlap-save SSB modulator (no WDSP TX). |
| WDSP wrapper | `web_control/wdsp_wrapper.py` | Optional `libwdsp.dylib` ctypes wrapper — RX only (AGC, NR2, NB, ANF). WDSP TX C-chain removed. |
| Frontend | `sunmrrc/static/` | `controls.js` (WebSockets + audio playback + waterfall), `mobile.js` (UI), modules |

### WebSocket endpoints

| Endpoint | Direction | Payload |
|----------|-----------|---------|
| `/WSCTRX` | Bidirectional text | Control commands (`setFreq:`, `setMode:`, `getWDSPStatus:`, etc.) |
| `/WSaudioRX` | Server → client binary | RX audio, 1-byte codec tag per frame: `0x00`=16 kHz Int16 PCM, `0x01`=Opus (16 kHz mono). Default Opus. |
| `/WSaudioTX` | Client → server binary | Opus-encoded TX audio from dedicated worker-side WebSocket (SharedArrayBuffer ring buffer path: AudioWorklet → SAB → Opus Worker → WS). 1-byte codec tag: `0x01`=Opus 16 kHz mono. |
| `/WSspectrum` | Server → client binary | 512-byte uint8 dB rows (0=-120dB, 255=0dB) |
| `/WSATR1000` | Bidirectional JSON | ATR-1000 tuner proxy (placeholder) |

### Control command flow

```
Browser sends "setFreq:7074000" over /WSCTRX
  → ws_ctrl() parses cmd:val
  → radio.set_frequency() or dsp_proc.demodulator.set_*()
  → Response broadcast to all ctrl_clients via _send_ctrl()
```

Mode is DSP-owned (AD-003): `setMode` updates `AudioDemodulator`, hardware stays mode-agnostic. Frequency is split into RX DDS (VFO + 30500 Hz IF offset) and TX VFO.

## TX power / drive (0x0017)

TX output power is set by the **DRIVE command (0x0017)**, NOT by software IQ gain. ALC is not supported on SunSDR2 firmware, so per-band drive is the only power control.

- **Byte mapping** (verified byte-for-byte against ExpertSDR3 TCI capture): `byte = round(255 × √(drive%/100))` — a square-root taper. 10%→0x50, 50%→0xb4, 100%→0xff.
- **Packet layout is the gotcha**: the drive byte goes in the **trailing word**, not the payload. `build_packet(CmdID.DRIVE, data=b"\0\0\0\0", trailing=byte)`. Putting it in the payload sends drive=0 (device transmits at near-zero power — this was the original "low power" bug).
- **Re-sent on every QSY**: ExpertSDR3 (and now `set_frequency()`) re-sends 0x0017 on each frequency change, because the device resets drive to a per-band calibration value otherwise. `set_ptt(tx=True)` also re-sends it just before keying as a guard.
- **Per-band power is user-configurable**, persisted to `band_power.json`, edited via `/api/band_power` and the **Band Power** menu panel. `band_power_for(freq_hz)` looks up the % for the current band; `BAND_POWER_DEFAULT` (100) covers out-of-band frequencies.
- **IQ amplitude must use the FULL scale** (`dsp.py` `TX_IQ_PEAK=1.0`, `TX_DRIVE_GAIN=2.8`): verified 2026-06-25 against a real ExpertSDR3 40m drive sweep (`device/captures/expert_40m_drive.pcap`) — ExpertSDR3's TX IQ peaks reach **1.0 full-scale** (voice RMS ~0.33), and IQ amplitude is **constant with drive** (drive only scales power at the device). The earlier `TX_IQ_PEAK=0.5` clipped half the amplitude through the tanh limiter and was THE root cause of low power (~20W vs ExpertSDR3's 45W at 100% drive on the same audio). The "~0.092 peak" figure in older notes was measured from a quiet/low-level capture segment and is **wrong** — ignore it. `TX_DRIVE_GAIN` was tuned from 3.0→3.5→2.8 to balance power vs. tanh limiter engagement.
- **The DRIVE byte is correct as-is** (verified against `tci_drive_scan.pcap`: 100%→trailing 255, byte in trailing word). Don't re-investigate it for power problems — the lever is `TX_IQ_PEAK` + drive %, not the byte format.
- **Client-side TX EQ gain staging**: `AudioTX_preamp = 1.5` (+3.5dB) in `tx_audio_eq.js`. The preamp was 3.0 but was reduced 2026-06-25 because the old value drove the AudioWorklet Int16 output to full scale (peak=1.0), which after Hilbert (+~30%) + server drive gain (×2.8) forced the tanh limiter to squash 75% of peak amplitude → heavy voice distortion. At ×1.5, the tanh engages only ~4% — clean SSB. Turn device drive ↑ for more power, not client gain. A 300Hz 4th-order Butterworth HPF (after DC blocker, before anti-alias LPF) improves SSB power efficiency from ~69% to ~96% by removing sub-300Hz energy that would otherwise eat amplifier headroom without contributing to intelligibility. See `SDD/diagrams/tx-gain-staging.svg`.

### TX power / voltage telemetry (0x1F00)

The device sends 0x1F00 (34 B) continuously, in RX and TX. **Field offsets reverse-engineered 2026-06-25** from `device/captures/expert_40m_drive.pcap` (a full 40m drive sweep) cross-checked against an external wattmeter and ExpertSDR3's own readout:

- `off30` **f32 = forward power in WATTS** (PEP envelope). Monotonic with drive: 28%→3W, 71%→54W, 88%→83W, 100%→101W. Matches ExpertSDR3's ~95W self-readout at 100%. **Direct float watts — NO fit needed.**
- `off16` **u16 = supply voltage × 10** (NOT SWR). Reads ~136 (13.6V) at idle, sags as power rises (0W→13.6, 30-50W→13.1, 80-110W→12.9V); correlation with forward power = **-0.79**, a textbook PSU-sag curve. Volts = `off16 / 10`.
- `off18` f32 = PA temperature °C (~42, barely moves).
- `off22` f32 = **average** forward power (ratio to off30 ≈ the 3:1 SSB crest factor) — not reverse power.
- `off26` f32 = always 1.0000 (device placeholder).

**The device exposes NO reverse-power field, so it cannot compute SWR.** The old code's "SWR = off16/100" was wrong — that's the 13.6V supply voltage. The old "off14 u16 + cubic fit" power was also wrong (off14 is non-monotonic noise). Frontend `getTXTelem` now carries `watts,volts,temp`; the UI **VOLT** field replaces the former **SWR** field.

## Dependencies

- **Python 3.12** virtualenv at `py311_env/` (symlinked as `venv/`)
- **Packages**: `fastapi`, `uvicorn[standard]`, `numpy`, `scipy`, `websockets`
- **Optional**: `libwdsp.dylib` at `/opt/homebrew/lib/` or `/usr/local/lib/` for NR2/AGC/noise blanker
- **Network**: Must be on same subnet as SunSDR2 DX (192.168.16.0/24)
- **TLS certs**: `sunmrrc/certs/fullchain.pem` + `sunmrrc/certs/radio.vlsc.net.key`

Install missing packages:
```bash
venv/bin/pip install fastapi uvicorn numpy scipy websockets
```

## Hardcoded network config

- Local IP: `192.168.16.100` (in `sunsdr_direct.py` and `server.py`)
- Device IP: `192.168.16.200`
- Control port: UDP 50001
- IQ stream port: UDP 50002

Changing deployment IP requires edits to `LOCAL_HOST` in `web_control/sunsdr_direct.py` and bind addresses in `sunmrrc/server.py`.

## IQ stream diagnostic

If `server.log` shows `IQ idle: pkt=0`, the SunSDR2 hardware is not sending IQ data (the server code is correct — all boot/heartbeat/keep-alive logic is in place). Possible causes:
- ExpertSDR3 not running on the network (device may need it for FPGA init)
- Device needs physical power cycle after repeated boot sequences
- Another client has claimed the stream

The IQ processing loop sends both heartbeat (0x0018 to port 50001 every 0.5s) and stream keep-alive (0xFFFE to port 50002 every 0.5s). When data flows, log shows `IQ stats: pkt=N iq=N spec=N audio=N`.

**IQ socket is pre-bound before the boot sequence** to avoid losing early packets — the UDP socket is created and bound to port 50002 before `boot_sequence()` sends HW_INIT. A **dedicated keep-alive thread** sends 0xFFFE independently of the asyncio event loop, ensuring the device never times out the stream even if the event loop is blocked.

## Key conventions

- **Restart script kills by cwd**, not by process name — won't accidentally kill `web_control/server.py` if it's running
- **TLS by default** for iOS secure context — `DISABLE_SSL=1` to force HTTP for local dev
- **WDSP is optional** (AD-008) — demods work without it; `get_wdsp_status()` reports `available: true/false`
- **WDSP RX-only** — NR2 (level 50), AGC SLOW active on startup. The frontend DSP toggle switches WDSP RX on/off. WDSP TX C-chain has been completely removed; Python Hilbert overlap-save is the sole SSB modulation path. No more `WDSPTXProcessor`, `setWDSPTXEnabled` commands, TX PROC UI panel, or TX symbol bindings.
- **NR2 level is now functional** (fixed 2026-06-24) — `SetRXAEMNRgainLine` applies the actual gain value (0.0=max NR2, 1.0=min). Previously `set_nr2_level()` computed the gain but never called the WDSP API.
- **process() chunked** (fixed 2026-06-24) — `WDSPProcessor.process()` now buffers variable-length input and drains in 256-sample blocks, returning full-length output. Previously it truncated input >256 samples.
- **Spectrum quantization is server-side** (AD-005) — waterfall canvas gets 512 uint8 values, not raw float dB
- **PTT release is safety-critical** (AD-007) — frontend has ACK retry + watchdog; backend has forced-RX handler on `s:` command
- **RX audio is tagged dual-codec** (AD-004) — each `/WSaudioRX` frame carries a 1-byte codec tag (`0x00`=Int16 PCM, `0x01`=Opus 16 kHz mono); default Opus (~18-24 kbps vs ~256 kbps PCM), switchable via `setOpus:` (Audio Codec menu). Server encodes via a direct `ctypes` libopus binding in `web_control/opus_rx.py` (NOT `opuslib` — arm64 macOS can't call the variadic `opus_encoder_ctl` through ctypes, so bitrate is set via the `max_data_bytes` cap on `opus_encode`). Falls back to PCM if libopus is missing. 16 kHz resampled server-side from 15625 Hz native rate
- **COEP header uses `credentialless`** (not `require-corp`) — `require-corp` breaks Worker `importScripts()` on Safari. `credentialless` enables `SharedArrayBuffer` while allowing workers to load their own sub-resources.
- **TX audio uses SharedArrayBuffer ring buffer** — `AudioWorklet` (tx_capture_worklet.js) writes float32 samples directly to SAB; Opus Worker (tx_opus_worker.js) polls SAB every 3ms, reads 320-sample frames, Opus-encodes, and sends via its own dedicated WebSocket to `/WSaudioTX`. Zero main-thread involvement after setup. Deleted `modules/tx_sab_ring.js` (dead code — SAB ring logic is embedded directly in worklet/worker).
- **Continuous DC blocker** — IIR highpass @ 20Hz replaces the old per-frame mean subtraction. Instant convergence, no DC wander across frame boundaries.
- **300Hz 4th-order Butterworth HPF** — inserted between DC blocker and anti-alias LPF in `feed_audio()`. Removes sub-300Hz energy that carries no voice intelligibility but consumes amplifier headroom; improves SSB power efficiency from ~69% to ~96%.
- **TX pacer: no de-prime** — once primed with 10 frames, drains every available frame immediately. Constants: `TX_MIC_PRIME_PKTS=60`, `TX_MIC_REPRIME_PKTS=20`.
- **IQ socket pre-bound before boot** — UDP socket bound to port 50002 before `boot_sequence()` sends HW_INIT, avoiding lost early packets. Dedicated keep-alive thread sends 0xFFFE independently of the asyncio event loop.

## Architecture decisions

See `SDD/08-architecture-decisions.md` for all 12 ADs (AD-001 through AD-012) with rationale. Key ones:
- FastAPI/Uvicorn over Tornado (AD-001)
- Direct UDP hardware control over TCI abstraction (AD-002)
- HTTPS/WSS required for mobile (AD-006)

## Known gaps (AD-009)

- TX SSB modulation **works** — Python Hilbert overlap-save is the sole SSB modulation path (WDSP TX C-chain removed). Audio path: SharedArrayBuffer ring buffer → Opus Worker → /WSaudioTX → server `feed_audio()` → continuous DC blocker (IIR 20Hz HPF) → 300Hz 4th-order Butterworth HPF → anti-alias LPF → Hilbert SSB modulator → tanh soft limiter → TX IQ. Confirmed on-air: Tune ~12W, voice 30–40W PEP via ATR-1000.
- **TX audio gain staging (AD-012)**: client preamp ×1.5 (`tx_audio_eq.js` `AudioTX_preamp`), server `TX_DRIVE_GAIN=2.8`, `TX_IQ_PEAK=1.0` with tanh soft limiter. The preamp was reduced from 3.0→1.5 on 2026-06-25 because the old value saturated the tanh (75% reduction → heavy distortion). `TX_DRIVE_GAIN` tuned 3.0→3.5→2.8. 300Hz HPF improves SSB power efficiency from ~69% to ~96%. See `SDD/diagrams/tx-gain-staging.svg`.
- **TX pacer**: `TX_MIC_PRIME_PKTS=60`, `TX_MIC_REPRIME_PKTS=20`. No de-prime — once primed with 10 frames, drains every available frame immediately.
- `/WSATR1000` accepts connections but doesn't interface with real tuner hardware
- `/api/mem_channels` implemented (GET/POST with JSON persistence to `mem_channels.json`)
- `/api/band_power` implemented (GET/POST with JSON persistence to `band_power.json`; frontend **Band Power** menu panel edits per-band drive %)
- Sample-rate selector (39/78/156/312 kHz) has frontend (**Sample Rate** menu) + backend: `setSampleRate:` → `radio.set_sample_rate()` → sends `0x0001` HW_INIT with word[11]=rate_index (0=39k, 1=78k, 2=156k, 3=312k) during a full re-boot sequence. **Verified 2025-06-24** via ExpertSDR3 capture analysis and direct device testing. The rate is set by `0x0001` (NOT `0x0020`), see `PROTOCOL.md` §4.3 and `sunsdr_direct.py` `build_hw_init()`. Rate change requires a full re-boot because 0x0001 must precede the frequency and stream-start commands.

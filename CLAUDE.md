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

## Architecture

See `SDD/09-architecture-overview.md` and `SDD/11-component-model.md` for detailed diagrams.

### Logical flow

```
Browser (iOS Safari)
  → HTTPS/WSS
  → FastAPI SunMRRC (sunmrrc/server.py)
    → SunSDR2DXClient (web_control/sunsdr_direct.py) — UDP control port 50001
    → StreamProcessor (web_control/dsp.py) — IQ → spectrum + audio
    → UDP IQ socket port 50002
    → SunSDR2 DX hardware @ 192.168.16.200
```

### Key modules

| Module | File | Role |
|--------|------|------|
| Server + WebSockets | `sunmrrc/server.py` | FastAPI app, lifespan, 5 WS endpoints, IQ processing loop, audio/spectrum broadcast |
| Radio client | `web_control/sunsdr_direct.py` | SunSDR2 DX UDP protocol — boot sequence, heartbeat, freq/PTT/AGC setters |
| DSP pipeline | `web_control/dsp.py` | `StreamProcessor` → `SpectrumProcessor` (FFT) + `AudioDemodulator` (SSB/AM/FM + WDSP) |
| WDSP wrapper | `web_control/wdsp_wrapper.py` | Optional `libwdsp.dylib` ctypes wrapper (AGC, NR2, NB, ANF) |
| Frontend | `sunmrrc/static/` | `controls.js` (WebSockets + audio playback + waterfall), `mobile.js` (UI), modules |

### WebSocket endpoints

| Endpoint | Direction | Payload |
|----------|-----------|---------|
| `/WSCTRX` | Bidirectional text | Control commands (`setFreq:`, `setMode:`, `getWDSPStatus:`, etc.) |
| `/WSaudioRX` | Server → client binary | 16 kHz Int16 PCM audio |
| `/WSaudioTX` | Client → server | TX audio placeholder (not yet consumed by backend) |
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

## Key conventions

- **Restart script kills by cwd**, not by process name — won't accidentally kill `web_control/server.py` if it's running
- **TLS by default** for iOS secure context — `DISABLE_SSL=1` to force HTTP for local dev
- **WDSP is optional** (AD-008) — demods work without it; `get_wdsp_status()` reports `available: true/false`
- **Spectrum quantization is server-side** (AD-005) — waterfall canvas gets 512 uint8 values, not raw float dB
- **PTT release is safety-critical** (AD-007) — frontend has ACK retry + watchdog; backend has forced-RX handler on `s:` command
- **Audio format is Int16 PCM** (AD-004) — Opus negotiation removed; 16 kHz resampled server-side from 15625 Hz native rate

## Architecture decisions

See `SDD/08-architecture-decisions.md` for all 9 ADs with rationale. Key ones:
- FastAPI/Uvicorn over Tornado (AD-001)
- Direct UDP hardware control over TCI abstraction (AD-002)
- HTTPS/WSS required for mobile (AD-006)

## Known gaps (AD-009)

- TX audio modulation not implemented (mic frames arrive but aren't consumed)
- `/WSATR1000` accepts connections but doesn't interface with real tuner hardware
- `/api/mem_channels` endpoint implemented (GET/POST with JSON persistence to `mem_channels.json`)
- CW/FT8/Recordings page links in menu are dead

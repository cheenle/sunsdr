# SunMRRC — SunSDR2 DX Mobile Radio Control  ![V1.0](https://img.shields.io/badge/version-1.0-blue)

Web-based mobile control server for the [SunSDR2 DX](https://eesdr.com/) SDR transceiver. Provides HTTPS/WSS access from any modern browser — full waterfall spectrum display, real-time RX audio, frequency/mode/filter control, WDSP noise reduction, and PTT management. Optimized for iPhone/iOS Safari.

## Quick start

```bash
cd sunmrrc
./restart.sh            # background, default port 8889
WEB_PORT=8080 ./restart.sh   # custom port
./restart.sh -f          # foreground (Ctrl-C to quit, live logs)
DISABLE_SSL=1 ./restart.sh   # HTTP debug mode (no TLS)
```

Open `https://localhost:8889` (or your custom port) in a browser. On iPhone, use the domain matching the TLS certificate (`https://radio.vlsc.net`).

Logs: `tail -f sunmrrc/server.log`

## How it works

```
iPhone Browser (iOS Safari)
  ↕ HTTPS / WSS
SunMRRC (FastAPI + Uvicorn)
  ↕ UDP 192.168.16.x
SunSDR2 DX Hardware
```

The server boots the SunSDR2 DX over UDP (port 50001), starts the IQ stream (port 50002), runs DSP demodulation server-side, and pushes spectrum frames + PCM audio to the browser over WebSockets. The browser renders a real-time waterfall and plays audio through Web Audio API.

## Features

| Feature | Status |
|---------|--------|
| Real-time waterfall (512-bin FFT, 38 Hz) | ✅ |
| RX audio (SSB/AM/FM/CW, Int16 PCM 16 kHz) | ✅ |
| Frequency & mode control (VFO, band, mode buttons) | ✅ |
| WDSP noise reduction (NR2, NB, ANF, AGC) | ✅ |
| S-meter with percentile-based signal level | ✅ |
| HTTPS/WSS with TLS (required for iOS audio) | ✅ |
| PTT with safety release (ACK retry, watchdog, forced-RX) | ✅ |
| Memory channels (6 slots, cookie backup) | ✅ |
| TX audio modulation | ✅ | Browser mic → SAB ring buffer → Opus → 300 Hz HPF → Python Hilbert SSB → 24-bit IQ; verified 37 dB SNR, 96% SSB efficiency, zero dropouts; IQ peak ~0.69, RMS ~0.68 at 100% drive |
| TX power / per-band drive | ✅ | Device DRIVE (0x0017), `/api/band_power` + Band Power UI |
| TX telemetry (power/voltage/temp) | ✅ | 0x1F00: off30 f32 forward watts (PEP), off16 u16/10 supply voltage, off18 f32 PA temp °C |
| Sample rate selector | ✅ | 39/78/156/312 kHz via 0x0001 HW_INIT |
| ATR-1000 antenna tuner integration | ❌ (placeholder) |
| CW / FT8 / Recordings pages | ❌ (menu stubs) |

## Architecture

### Backend (`sunmrrc/server.py`)

FastAPI app with 5 WebSocket endpoints:

| Endpoint | Type | Purpose |
|----------|------|---------|
| `/WSCTRX` | Text | Control commands (frequency, mode, PTT, WDSP, filters) |
| `/WSaudioRX` | Binary | RX audio — tagged dual-codec: 0x00=Int16 PCM, 0x01=Opus (16 kHz mono) |
| `/WSaudioTX` | Binary | TX microphone — tagged Opus/PCM uplink → Hilbert SSB modulator |
| `/WSspectrum` | Binary | 512-byte uint8 spectrum rows for waterfall canvas |
| `/WSATR1000` | JSON | ATR-1000 tuner proxy (accepts connections, HW integration TBD) |

The IQ processing loop receives ~390 packets/sec of 24-bit I/Q samples from the device, feeds them through `StreamProcessor` → `SpectrumProcessor` (FFT) + `AudioDemodulator` (SSB/AM/FM/WDSP), then broadcasts spectrum and tagged audio (Opus/PCM) to connected clients.

### Shared libraries (`web_control/`)

| Module | Role |
|--------|------|
| `sunsdr_direct.py` | SunSDR2 DX UDP protocol — boot sequence, heartbeat (0x0018 every 0.5s), frequency/PTT/AGC setters |
| `dsp.py` | DSP pipeline — `StreamProcessor`, `SpectrumProcessor` (FFT), `AudioDemodulator` (SSB/AM/FM + WDSP) |
| `wdsp_wrapper.py` | Optional `libwdsp.dylib` ctypes wrapper — AGC, NR2, noise blanker, auto notch |
| `gr4_filters.py` | FIR filter design for SSB bandpass and lowpass filters |

### Frontend (`sunmrrc/static/`)

| File | Role |
|------|------|
| `controls.js` | Core WebSocket setup, RX audio decode/playback, waterfall rendering (adaptive noise floor + color ramp), S-meter |
| `mobile.js` | Mobile UX — band/mode buttons, DSP panel, memory manager, ATR hooks |
| `tx_button.js` | Touch PTT with lock handling, watchdog, warm-up frames |
| `mobile.css` | Full mobile layout — safe areas, controls, panels, waterfall canvas |
| `modules/` | PTT manager, settings manager, TX audio EQ, Tune/CQ, Opus codec |

See `SDD/09-architecture-overview.md` for detailed diagrams and `SDD/11-component-model.md` for the full component inventory.

## Requirements

- **Python 3.12** with packages: `fastapi`, `uvicorn[standard]`, `numpy`, `scipy`, `websockets`
- **SunSDR2 DX** on the same LAN subnet (default: 192.168.16.200)
- **macOS** or Linux with network interface at 192.168.16.100
- **TLS certificate** pair at `sunmrrc/certs/fullchain.pem` + `sunmrrc/certs/radio.vlsc.net.key`
- **iOS 15+** for mobile use (Safari requires HTTPS secure context for `getUserMedia` and AudioContext)

Optional: `libwdsp.dylib` at `/opt/homebrew/lib/` or `/usr/local/lib/` for hardware-accelerated NR2/AGC/noise blanker.

A Python virtual environment is expected at `py311_env/` (symlinked as `venv/`). Install dependencies:

```bash
venv/bin/pip install fastapi uvicorn numpy scipy websockets
```

## Network configuration

All IP addresses are currently hardcoded for the development LAN:

| Setting | Value | File |
|---------|-------|------|
| Local PC IP | `192.168.16.100` | `sunsdr_direct.py`, `server.py` |
| SunSDR2 DX IP | `192.168.16.200` | `sunsdr_direct.py` |
| Control port | UDP 50001 | `sunsdr_direct.py` |
| IQ stream port | UDP 50002 | `server.py` |
| Web server (default) | HTTPS 8889 | `restart.sh` |

To deploy on a different subnet, update these values and rebuild the certificates.

## Key architecture decisions

Documented in `SDD/08-architecture-decisions.md`:

- **AD-001**: FastAPI/Uvicorn over Tornado — small surface, async-native
- **AD-002**: Direct SunSDR2 DX UDP protocol — no generic Hamlib abstraction
- **AD-003**: Mode is DSP-owned — hardware stays IQ/mode-agnostic
- **AD-004**: Tagged dual-codec audio transport (Opus + Int16 PCM) — 1-byte codec tag per frame, 0x00=PCM, 0x01=Opus. Default Opus (~18-24 kbps); switchable via Audio Codec menu.
- **AD-005**: Server-side spectrum quantization — 512 uint8 bins for efficient transfer
- **AD-006**: HTTPS/WSS by default — required for iOS Safari secure context
- **AD-007**: PTT release as safety-critical flow — ACK retry, watchdog, backup forced-RX
- **AD-008**: WDSP is optional and runtime-toggled — RX works without `libwdsp`
- **AD-009**: Frontend hooks preserved, missing backends explicitly documented

## Troubleshooting

**No waterfall / no audio**  
Check `sunmrrc/server.log`. If you see `IQ idle: pkt=0`, the SunSDR2 hardware is not streaming IQ data. The server boots correctly — this is a device-side issue. Common causes:
- ExpertSDR3 not running on the network (device may need it for FPGA initialization)
- Device needs physical power cycle after repeated boot sequences
- Another client has claimed the IQ stream

**iPhone no sound / no mic prompt**  
Ensure you're using HTTPS (`https://radio.vlsc.net` or `https://<ip>`). iOS Safari does not expose `getUserMedia` or reliable AudioContext to HTTP origins. Set `DISABLE_SSL=1` only for local desktop debugging.

**Port conflicts**  
`restart.sh` kills old processes by working directory (not process name), so it won't accidentally kill other `server.py` instances. If ports are stuck, run manually:

```bash
lsof -ti :8889 :50001 :50002 | xargs kill -9
```

## Project status

See `STATUS.md` for the latest phased progress and `SDD/` for full architecture documentation.

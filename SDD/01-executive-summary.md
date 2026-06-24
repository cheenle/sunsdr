# 1. Executive Summary

## 1.1 Project Overview

SunMRRC is a mobile-first browser remote-control system for SunSDR2 DX. It provides a web UI, WebSocket control/audio channels, direct UDP communication with the radio, IQ stream demodulation, browser audio playback, S-meter estimation, and a compact waterfall display.

The current codebase is intentionally narrower than the older MRRC documentation it replaced. It is no longer a Tornado/Hamlib/PyAudio-centric design; it is a FastAPI/Uvicorn service that imports shared SunSDR2 DX protocol and DSP modules from `../web_control` and serves a dedicated mobile UI from `static/`.

## 1.2 Current Design Goals

| Goal | Target | Current Evidence |
|------|--------|------------------|
| Mobile-first operation | iPhone/mobile browser as primary UI | `static/index.html`, `static/mobile.css`, `static/mobile.js` |
| Secure iOS runtime | HTTPS/WSS secure context | `_find_ssl()` in `server.py`, `STATUS.md` HTTPS notes |
| Direct SunSDR control | No rigctld dependency for this app | `SunSDR2DXClient` imported from `../web_control/sunsdr_direct.py` |
| Real-time RX | IQ UDP to browser speaker | `_process_iq_stream()`, `StreamProcessor`, `/WSaudioRX` |
| Visual signal awareness | S-meter (exponentially smoothed) plus accumulated/contrast-stretched waterfall | `getSignalLevel:*`, `/WSspectrum`, `waterfall-canvas` |
| Safe PTT handling | Avoid stuck TX states | `ptt_manager.js`, `tx_button.js`, `setPTT` handling |
| DSP configurability | Runtime WDSP controls | `setWDSP*` commands and `AudioDemodulator` WDSP setters |

## 1.3 Implemented Core Features

| Feature | Status | Description |
|---------|--------|-------------|
| Static mobile PWA-style UI | Implemented | Mobile layout with safe-area support, manifest, service worker, bottom PTT area |
| Control WebSocket | Implemented | `/WSCTRX` handles `PING`, frequency, mode, PTT, tune, gain, filter, WDSP commands |
| RX audio WebSocket | Implemented | `/WSaudioRX` broadcasts tagged dual-codec frames (0x00=PCM, 0x01=Opus 16 kHz mono); default Opus |
| TX voice modulation | Implemented | `/WSaudioTX` mic frames → Hilbert SSB → 24-bit IQ → `0xFFFD` TX stream; on-air verified (Tune ~12 W, voice 30–40 W PEP) |
| TX power / drive control | Implemented | Device DRIVE command (`0x0017`) with per-band power; confirmed on-air |
| Per-band power UI | Implemented | Menu → Band Power panel + `/api/band_power` (persisted to `band_power.json`) |
| Spectrum WebSocket | Implemented | `/WSspectrum` broadcasts compact uint8 FFT rows; browser accumulates frames (~38 Hz → ~3.8 Hz) and renders with adaptive noise-floor contrast |
| SunSDR2 DX UDP control | Implemented | Boot/connect sequence and parameter commands through shared direct protocol module |
| IQ stream processing | Implemented | UDP `50002` IQ decode, DSP feed, audio extraction, spectrum extraction |
| Memory channel API | Implemented | `/api/mem_channels` GET/POST with JSON persistence (`mem_channels.json`) |
| WDSP runtime control | Conditional | Active when `libwdsp` is available through `wdsp_wrapper` |
| HTTPS auto-start | Implemented | Uvicorn starts with TLS when cert/key files exist |
| Restart automation | Implemented | `restart.sh` cleans exact working-directory server process and listening port |

## 1.4 Explicit Capability Boundaries

| Capability | Boundary |
|------------|----------|
| TX power telemetry | Device sends `0x1F00` in all modes (verified: 273 TX-state packets in captures). off16 u16/100 = SWR, off14 u16 = pwr_raw (cubic fit → watts), off18 f32 = temp °C |
| ATR-1000 | Frontend hooks and status placeholders exist; `/WSATR1000` endpoint accepts connections but does not interface with real tuner hardware |
| Recordings | `recordings.html` page exists; CW/FT8 menu links were removed (target pages absent) |
| Authentication | Cookie/callsign helpers exist; no server-side auth boundary is implemented in `server.py` |

## 1.5 Architecture Layers

```text
Client Layer
  Mobile browser UI, Web Audio API, WebSocket clients, service worker

Application Layer
  FastAPI app, static file serving, WebSocket endpoints, client fan-out

Signal Layer
  IQ packet intake, spectrum processor, audio demodulator, optional WDSP chain

Device Layer
  SunSDR2 DX UDP control and stream protocol
```

## 1.6 Current Project Status

As of 2026-06-23, the RX chain is healthy and the iOS secure-context blocker is resolved via HTTPS/WSS startup (expected mobile entry `https://radio.vlsc.net:8080`). The TX voice path is implemented end-to-end and on-air verified: browser mic → Hilbert SSB → 24-bit IQ, with output power set by the device DRIVE command (`0x0017`) per band (Tune ~12 W, voice 30–40 W PEP read from the ATR-1000). Remaining open work is control-network configurability (fixed LAN IPs) and the ATR-1000 backend endpoint.

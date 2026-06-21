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
| RX audio WebSocket | Implemented | `/WSaudioRX` broadcasts 16 kHz Int16 PCM frames to browser clients |
| TX audio WebSocket | Transport only | `/WSaudioTX` accepts connections; server currently discards received frames |
| Spectrum WebSocket | Implemented | `/WSspectrum` broadcasts compact uint8 FFT rows; browser accumulates frames (~38 Hz → ~3.8 Hz) and renders with adaptive noise-floor contrast |
| SunSDR2 DX UDP control | Implemented | Boot/connect sequence and parameter commands through shared direct protocol module |
| IQ stream processing | Implemented | UDP `50002` IQ decode, DSP feed, audio extraction, spectrum extraction |
| WDSP runtime control | Conditional | Active when `libwdsp` is available through `wdsp_wrapper` |
| HTTPS auto-start | Implemented | Uvicorn starts with TLS when cert/key files exist |
| Restart automation | Implemented | `restart.sh` cleans exact working-directory server process and listening port |

## 1.4 Explicit Capability Boundaries

| Capability | Boundary |
|------------|----------|
| TX voice | PTT and tune control exist, but microphone audio is not yet modulated into the SunSDR TX stream |
| ATR-1000 | Frontend hooks and status placeholders exist; `/WSATR1000` server endpoint exists as placeholder without hardware interface |
| Memory channels | Frontend service-oriented manager exists; `/api/mem_channels` backend implemented (GET/POST with JSON persistence) |
| CW/FT8/recordings | Navigation links exist, but target pages are not present in this repository snapshot |
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

As of 2026-06-21, the RX chain is healthy on local desktop and the mobile-side blocker was traced to iOS secure-context requirements. HTTPS/WSS startup is implemented and the expected mobile entry is `https://radio.vlsc.net:8889`. Control-network tuning and TX microphone modulation remain open engineering work.

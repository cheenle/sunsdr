# 2. Business Direction (BUS 411)

## 2.1 Vision

Make SunSDR2 DX usable from a phone browser with the least operational friction: open a secure URL, hear RX audio, see signal context, control essential radio parameters, and safely key/de-key the transmitter.

## 2.2 Mission

Deliver a pragmatic, browser-native remote radio control surface for HAM operation that favors direct SunSDR integration, mobile ergonomics, low latency, and field maintainability over heavyweight framework or native-app complexity.

## 2.3 Business Goals

| ID | Goal | Description |
|----|------|-------------|
| G1 | Mobile RX confidence | Operator can reliably listen to SunSDR2 DX from iPhone/mobile browser |
| G2 | Safe remote control | Frequency, mode, PTT, tune, gain, and DSP controls behave predictably |
| G3 | Minimal deployment | Single Python process serves UI, WebSockets, TLS, and radio bridge |
| G4 | Design continuity | SDD records implementation facts, decisions, risks, and future work |
| G5 | Incremental extensibility | Preserve hooks for ATR and digital modes without overstating completion |

## 2.4 Objectives

| ID | Objective | Target | Current Status |
|----|-----------|--------|----------------|
| O1 | iOS secure-context operation | HTTPS/WSS entry works | Implemented by TLS auto-detect |
| O2 | RX audio availability | Browser receives continuous tagged dual-codec audio (Opus default, Int16 PCM fallback) | Implemented |
| O3 | Control-plane liveness | `PING`/`PONG`, state query and command response | Implemented |
| O4 | PTT release safety | Multiple release safeguards | Implemented in frontend and backend backup command |
| O5 | Spectrum visibility | Compact waterfall stream | Implemented |
| O6 | TX voice completion | Browser mic to SunSDR TX modulation | Implemented (on-air verified) |
| O7 | ATR integration | Backend endpoint and device bridge | Open |
| O8 | TX power control | Per-band drive (`0x0017`) configurable from UI | Implemented (on-air verified) |

## 2.5 Strategy

| ID | Strategy | Description |
|----|----------|-------------|
| S1 | Mobile-first UI | Optimize controls for touch, safe areas, one-screen radio operation |
| S2 | Direct radio protocol | Use verified SunSDR2 DX UDP protocol instead of generic CAT where possible |
| S3 | Browser-native audio | Keep RX playback in Web Audio; avoid native app dependency |
| S4 | Small service surface | FastAPI app owns static files and WebSockets in one process |
| S5 | Document actual state | SDD distinguishes implemented features from inherited/planned UI hooks |

## 2.6 Tactics

| ID | Tactic | Implementation |
|----|--------|----------------|
| T1 | HTTPS by default | `_find_ssl()` selects cert/key, `DISABLE_SSL=1` only for local HTTP |
| T2 | WSS auto-selection | Frontend uses `location.protocol` to choose `wss://` or `ws://` |
| T3 | Direct IQ demodulation | UDP `50002` IQ packets feed `StreamProcessor` |
| T4 | Compact waterfall | Server quantizes spectrum to uint8 frames before broadcast |
| T5 | PTT release ACK loop | Frontend retries `setPTT:false` and uses backup `s:` command |
| T6 | Optional WDSP | Runtime setters are no-op safe when `libwdsp` is unavailable |

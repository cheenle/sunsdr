# 3. Project Definition (ENG 343)

## 3.1 Project Attributes

| Attribute | Value |
|-----------|-------|
| Project Name | SunMRRC |
| Project Type | Mobile remote radio control for SunSDR2 DX |
| Primary Users | HAM operators using phone or desktop browsers |
| Primary Radio | SunSDR2 DX |
| Server Platform | macOS/Linux with Python 3.12+ |
| Client Platform | Modern browser, especially iOS Safari and mobile Chrome |
| Runtime Framework | FastAPI + Uvicorn |
| Frontend Stack | HTML/CSS/vanilla JavaScript/Web Audio API |
| Repository Scope | `sunmrrc/` plus shared imports from `../web_control/` |

## 3.2 In Scope

- Serve a mobile-first web UI from `static/`.
- Maintain WebSocket control channel `/WSCTRX`.
- Maintain RX audio channel `/WSaudioRX` using Int16 PCM frames.
- Maintain placeholder TX audio socket `/WSaudioTX` for browser microphone pipeline integration.
- Maintain waterfall channel `/WSspectrum` with compact FFT frames.
- Connect to SunSDR2 DX over direct UDP protocol.
- Receive and parse IQ stream packets from local bind address `192.168.16.100:50002`.
- Demodulate IQ to audio and compute spectrum through shared DSP components.
- Support DSP mode changes and WDSP runtime toggles where shared DSP supports them.
- Start with HTTPS automatically when certificate files are present.
- Provide restart/start scripts for local operation.
- Preserve design notes, decisions, risks, and open gaps in SDD.

## 3.3 Out of Scope for Current Baseline

- Native iOS/Android application.
- Cloud-hosted multi-tenant service.
- Full authentication/authorization backend.
- Complete TX microphone modulation into SunSDR TX IQ/audio path.
- ATR-1000 backend endpoint and proxy in this repository.
- CW, FT8, recordings, and logbook pages unless added later.
- General Hamlib rig abstraction; this codebase is SunSDR2 DX direct-control oriented.

## 3.4 Success Criteria

| ID | Criterion | Verification |
|----|-----------|--------------|
| SC1 | HTTPS entry starts when certs exist | Server log shows `sunmrrc https://0.0.0.0:<port>` |
| SC2 | iPhone can request microphone permissions | Page loaded from `https://radio.vlsc.net:8080` secure context |
| SC3 | RX audio arrives at browser | `/WSaudioRX` receives Int16 PCM frames and UI bitrate updates |
| SC4 | Radio frequency control works | `setFreq:*` returns `getFreq:*` and radio state changes |
| SC5 | DSP mode changes are local and explicit | `setMode:*` updates demodulator mode and returns `getMode:*` |
| SC6 | PTT release cannot silently stick indefinitely | Release ACK retry, backup `s:`, watchdog, and backend forced RX path exist |
| SC7 | Waterfall stream remains lightweight | `/WSspectrum` sends 512-byte quantized rows |

## 3.5 Major Milestones

| Milestone | Date | Deliverable |
|-----------|------|-------------|
| M1 | 2026-06 | Establish SunMRRC mobile server and static UI baseline |
| M2 | 2026-06 | Stabilize RX chain with shared SunSDR DSP import |
| M3 | 2026-06 | Add HTTPS/WSS startup to satisfy iOS secure-context requirements |
| M4 | 2026-06 | Consolidate WDSP controls into main mobile DSP panel |
| M5 | Planned | Complete TX microphone modulation path |
| M6 | Planned | Implement backend memory-channel API or remove frontend dependency |
| M7 | Planned | Decide whether ATR/CW/FT8 links are in-scope for SunMRRC or should be removed |

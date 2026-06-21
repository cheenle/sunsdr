# SunMRRC SDD - Software Design Description

> SunSDR2 DX Mobile Remote Radio Control
> IBM Team Solution Design (TeamSD) v2.3.2 aligned documentation set

## Purpose

This SDD is the canonical design record for the `sunmrrc` codebase. It captures the current requirements, architecture, design decisions, component boundaries, operational model, capability inventory, known gaps, and evolution history for the mobile-first SunSDR2 DX remote control implementation.

Runtime facts are derived from the current repository, primarily `server.py`, `static/index.html`, `static/controls.js`, `static/mobile.js`, `static/modules/*`, `static/tx_button.js`, `restart.sh`, `start.sh`, and the imported shared SunSDR implementation under `../web_control/`.

## Document Index

| # | Chapter | ART Code | File |
|---|---------|----------|------|
| 1 | Executive Summary | - | [01-executive-summary.md](01-executive-summary.md) |
| 2 | Business Direction | BUS 411 | [02-business-direction.md](02-business-direction.md) |
| 3 | Project Definition | ENG 343 | [03-project-definition.md](03-project-definition.md) |
| 4 | System Context | APP 011 | [04-system-context.md](04-system-context.md) |
| 5 | Non-Functional Requirements | ART 0507 | [05-non-functional-requirements.md](05-non-functional-requirements.md) |
| 6 | Use Case Model | ART 0508 | [06-use-case-model.md](06-use-case-model.md) |
| 7 | Subject Area Model | APP 408 | [07-subject-area-model.md](07-subject-area-model.md) |
| 8 | Architecture Decisions | ART 0513 | [08-architecture-decisions.md](08-architecture-decisions.md) |
| 9 | Architecture Overview | ART 0512 | [09-architecture-overview.md](09-architecture-overview.md) |
| 10 | Service Model | ART 0582 | [10-service-model.md](10-service-model.md) |
| 11 | Component Model | ART 0515 | [11-component-model.md](11-component-model.md) |
| 12 | Operational Model | ART 0522 | [12-operational-model.md](12-operational-model.md) |
| 13 | Feasibility Assessment | ART 0530 | [13-feasibility-assessment.md](13-feasibility-assessment.md) |
| 14 | Version History | - | [14-version-history.md](14-version-history.md) |

## Quick Facts

| Attribute | Value |
|-----------|-------|
| Document ID | SDD-SUNMRRC-2026-001 |
| SDD Version | V3.0 |
| Baseline Date | 2026-06-21 |
| Status | Living design baseline |
| Project | SunMRRC |
| Primary Radio | SunSDR2 DX |
| Runtime | Python 3.12+, FastAPI, Uvicorn, NumPy |
| Frontend | HTML5, CSS3, vanilla JavaScript, Web Audio API |
| Transport | HTTPS/WSS for browser, UDP for SunSDR2 DX |
| Default Production Entry | `https://radio.vlsc.net:8080` |

## System at a Glance

```text
Browser mobile UI
  | HTTPS static assets
  | WSS: /WSCTRX, /WSaudioRX, /WSaudioTX, /WSspectrum
  v
FastAPI/Uvicorn sunmrrc server
  | imports shared SunSDR protocol + DSP code from ../web_control
  | UDP control :50001, UDP IQ stream :50002
  v
SunSDR2 DX hardware
```

## Capability Summary

| Area | Status | Notes |
|------|--------|-------|
| Mobile UI | Implemented | `static/index.html`, `mobile.css`, `mobile.js` |
| RX audio | Implemented | IQ demodulation to Int16 PCM over `/WSaudioRX` |
| Spectrum waterfall | Implemented | Quantized 512-bin frames over `/WSspectrum` |
| Radio control | Implemented | Frequency, DSP mode, PTT, tune, gain, filter, AGC/preamp controls |
| WDSP controls | Implemented when libwdsp is available | NR2, NB, ANF, NF, AGC, notches |
| HTTPS/WSS | Implemented | Auto-detects `certs/fullchain.pem` and `certs/radio.vlsc.net.key`; `DISABLE_SSL=1` for HTTP |
| TX microphone modulation | Not yet complete | `/WSaudioTX` receives frames, but server-side audio modulation into SunSDR TX path is unresolved |
| ATR-1000 | Frontend legacy/planned | UI references exist; backend `/WSATR1000` is not implemented in this codebase |
| CW/FT8/recordings links | Frontend legacy/planned | Menu links exist; corresponding pages are not present in this repository snapshot |
| Memory channel API | Frontend planned | `MemoryChannelManager` expects `/api/mem_channels`; backend endpoint is not present |

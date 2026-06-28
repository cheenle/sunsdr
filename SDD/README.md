# SunMRRC SDD - Software Design Description

> SunSDR2 DX Mobile Remote Radio Control
> IBM Team Solution Design (TeamSD) v2.3.2 aligned documentation set

## Purpose

This SDD is the canonical design record for the `sunmrrc` codebase. It captures the current requirements, architecture, design decisions, component boundaries, operational model, capability inventory, known gaps, and evolution history for the mobile-first SunSDR2 DX remote control implementation.

Runtime facts are derived from the current repository, primarily `server.py`, `static/index.html`, `static/controls.js`, `static/mobile.js`, `static/modules/*`, `static/tx_button.js`, `restart.sh`, and the imported shared SunSDR implementation under `../web_control/`.

## Document Index

> 📥 **Presentation**: [SunMRRC-Architecture-SDD-V3.4.pptx](SunMRRC-Architecture-SDD-V3.4.pptx) — 13-slide architecture overview PPTX (dark minimal, 12 ADs, gain staging, telemetry, SVG diagrams)

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
| 15 | PTT Safety Architecture | ART 0535 | [15-ptt-safety-architecture.md](15-ptt-safety-architecture.md) |

## Quick Facts

| Attribute | Value |
|-----------|-------|
| Document ID | SDD-SUNMRRC-2026-001 |
| SDD Version | V3.5 (SunMRRC V1.0) |
| Baseline Date | 2026-06-26 |
| Status | Production release |
| Project | SunMRRC |
| Primary Radio | SunSDR2 DX |
| Runtime | Python 3.12+, FastAPI, Uvicorn, NumPy |
| Frontend | HTML5, CSS3, vanilla JavaScript, Web Audio API |
| Transport | HTTPS/WSS for browser, UDP for SunSDR2 DX |
| Default Production Entry | `https://radio.vlsc.net:8889` |

## System at a Glance

> See [`diagrams/system-architecture.svg`](diagrams/system-architecture.svg) for a detailed visual.

```text
Browser mobile UI  (iOS Safari / Chrome)
  | HTTPS static assets · WSS: /WSCTRX /WSaudioRX /WSaudioTX /WSspectrum
  v
FastAPI/Uvicorn sunmrrc server  (server.py + ../web_control/)
  | SunSDR2DXClient · StreamProcessor · TXModulator · Opus Codec · Auth
  | UDP control :50001 · UDP IQ stream :50002
  v
SunSDR2 DX Hardware  (192.168.16.200)
  | DRIVE (0x0017) per-band power · 0x1F00 telemetry (W/V/°C)
```

## Architecture Diagrams

SVG diagrams are in [`diagrams/`](diagrams/):

| Diagram | File | Description |
|---------|------|-------------|
| System Architecture | [`diagrams/system-architecture.svg`](diagrams/system-architecture.svg) | Full system: browser → server → DSP → SunSDR2 DX. All WebSocket endpoints, components, protocols. |
| TX Gain Staging | [`diagrams/tx-gain-staging.svg`](diagrams/tx-gain-staging.svg) | End-to-end TX audio chain with gain values at each stage, client EQ pipeline, server Hilbert/drive/tanh, healthy vs distorted comparison. AD-012. |
| Telemetry Flow | [`diagrams/telemetry-flow.svg`](diagrams/telemetry-flow.svg) | 0x1F00 packet layout with verified field offsets (off30=W, off16=V, off18=°C). Documents what was corrected on 2026-06-25. |

## Capability Summary

| Area | Status | Notes |
|------|--------|-------|
| Mobile UI | Implemented | `static/index.html`, `mobile.css`, `mobile.js` |
| RX audio | Implemented | IQ demodulation to tagged dual-codec (Opus/PCM) over `/WSaudioRX` |
| Spectrum waterfall | Implemented | Quantized 512-bin frames over `/WSspectrum` |
| Radio control | Implemented | Frequency, DSP mode, PTT, tune, gain, filter, AGC/preamp controls |
| WDSP controls | Implemented when libwdsp is available | NR2, NB, ANF, NF, AGC, notches |
| HTTPS/WSS | Implemented | Auto-detects `certs/fullchain.pem` and `certs/radio.vlsc.net.key`; `DISABLE_SSL=1` for HTTP |
| TX voice modulation | Implemented | `/WSaudioTX` mic PCM → Hilbert SSB → 24-bit IQ → `0xFFFD` TX stream. Gain-staged: client preamp ×1.5, server TX_DRIVE_GAIN ×3.0, TX_IQ_PEAK=1.0, tanh soft limiter. On-air verified. |
| TX power / drive | Implemented | Device DRIVE command (`0x0017`), runtime per-band power via `/api/band_power` + Band Power menu panel (persisted to `band_power.json`) |
| TX audio EQ | Implemented | Client-side Web Audio EQ presets (DEFAULT/MEDIUM/STRONG/RAGCHEW), compressor 3:1, anti-alias lowpass. Gain-staged per AD-012. |
| TX telemetry (0x1F00) | Implemented | off30 f32 = forward watts, off16 u16/10 = supply volts, off18 f32 = PA temp °C. Device has NO reverse-power → no SWR (use ATR-1000). |
| Memory channel API | Implemented | `/api/mem_channels` GET/POST with JSON persistence (`mem_channels.json`) |
| Recordings | Implemented | `recordings.html` + server-side RX MP3 capture/download (`/api/recordings`) |
| Session authentication | Implemented | Shared-password login (`/login` → `/api/auth/login`); `_auth_tokens` + `sunmrrc_auth` cookie (30-day); all routes and WS endpoints gated; WS uses `?token=` query param |
| ATR-1000 | Frontend legacy/planned | UI references exist; backend `/WSATR1000` is not implemented in this codebase |
| CW/FT8 menu links | Removed | Dead links cleaned from the menu; pages not present in this repository snapshot. Recordings page is present (`recordings.html`) |

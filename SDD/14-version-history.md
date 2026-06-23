# 14. Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| SDD V1.0 | 2026-03-15 | MRRC Team | Original MRRC TeamSD document set |
| SDD V2.0 | 2026-05-02 | MRRC Team | MRRC V5.0 mobile UI and broader MRRC capability update |
| SDD V2.1 | 2026-05-10 | MRRC Team | MRRC V5.1 audio preset and Web Audio notes |
| SDD V2.2 | 2026-05-18 | MRRC Team | MRRC V5.2 playback and WDSP notes |
| SDD V3.0 | 2026-06-21 | OpenCode | Re-baselined entire SDD to current `sunmrrc` codebase: FastAPI/Uvicorn, SunSDR2 DX direct UDP, mobile RX/control, HTTPS/WSS, explicit gaps |
| SDD V3.1 | 2026-06-21 | OpenCode | Waterfall rendering (frame accumulation + adaptive noise floor), S-meter exponential smoothing, PING→PONG latency fix, removed duplicate `/WSspectrum` route |
| SDD V3.2 | 2026-06-23 | Claude | TX voice modulation and TX power control documented as implemented: device DRIVE (`0x0017`) per-band power, `/api/band_power` + Band Power UI, `/api/mem_channels` graduated to implemented, AD-010 added, CW/FT8 dead links removed |

## Key Changes in SDD V3.2

| Chapter | Change |
|---------|--------|
| 1 | Core features: TX voice modulation, TX power/drive control, per-band power UI, memory channel API moved to Implemented; capability boundaries trimmed to ATR + CW/FT8 only |
| 2 | O6 (TX voice) marked implemented/on-air-verified; added O8 (per-band TX power); G5 narrowed to ATR/digital modes |
| 3 | In-scope adds TX uplink, DRIVE power, `/api/band_power`, `/api/mem_channels`; out-of-scope drops TX modulation; SC8 + milestones updated |
| 4 | `/WSaudioTX` interface re-described as active mic uplink; added TX modulation + DRIVE data flows |
| 5 | NFR-063 (TX audio quality) defined with on-air-verified targets |
| 6 | UC-007 (mic TX) and UC-008 (memory channels) marked implemented |
| 7 | TXModulationFrame + MemoryChannel promoted to real entities; added BandPowerConfig; band_power.json/mem_channels.json persistence |
| 8 | AD-009 reworded (TX + memory API no longer gated); added AD-010 (TX power via DRIVE 0x0017) |
| 9 | WS table + command architecture add setDrive/`/api/band_power`; known gaps trimmed |
| 10 | TXAudioIngressService + MemoryChannelService marked implemented; added BandPowerService; contract adds setDrive |
| 11 | TXAudioWebSocket re-described; added TXModulator + TxCaptureWorklet; gaps trimmed |
| 12 | Connection matrix TX path; band_power.json config; TX verification procedure + telemetry blind-spot note |
| 13 | R5/R7 + I2/I4 resolved; added TX power root-cause issue (resolved) |

## Key Changes in SDD V3.1

| Chapter | Change |
|---------|--------|
| 6 | UC-004 expanded: waterfall frame accumulation (10 frames → ~3.8 Hz), adaptive per-row noise floor (30th percentile), blue-sea bias + contrast gain, asymmetric S-meter smoothing; UC-001 notes `Waterfall_start/stop` on power on/off |
| 5 | New NFRs for waterfall render smoothness and S-meter needle stability |
| 9 | Frontend architecture notes the `controls.js` waterfall render module and `updateSMeter` smoothing |
| 13 | I6 (duplicate `/WSspectrum` route) marked resolved; PING→PONG latency `--ms` bug noted resolved |

## Key Changes in SDD V3.0

| Chapter | Change |
|---------|--------|
| README | Replaced MRRC/Tornado/Hamlib quick facts with SunMRRC/FastAPI/SunSDR2 DX facts |
| 1 | Reframed executive summary around current RX/control baseline and open TX/ATR gaps |
| 2 | Updated business direction to mobile SunSDR operation and design continuity |
| 3 | Rebuilt project definition with current scope, out-of-scope items, and success criteria |
| 4 | Rebuilt system context around browser, FastAPI server, shared DSP imports, SunSDR UDP interfaces |
| 5 | Updated NFRs for HTTPS, RX audio, PTT safety, operability, and maintainability |
| 6 | Rebuilt use cases for session startup, RX, tune/mode, spectrum, PTT/tune, DSP controls |
| 7 | Rebuilt subject model around sessions, radio state, IQ, RX audio, spectrum, DSP, TLS |
| 8 | Replaced outdated architecture decisions with current FastAPI/direct-UDP/Int16/HTTPS/PTT decisions |
| 9 | Rebuilt architecture overview with current backend, frontend, WebSocket, signal, and deployment views |
| 10 | Rebuilt service model and service contracts for current endpoints |
| 11 | Rebuilt component model with actual local files and shared imported modules |
| 12 | Rebuilt operational model around `restart.sh`, TLS, fixed LAN addresses, logs, and procedures |
| 13 | Rebuilt feasibility assessment with current risks and open issues |
| 14 | Added this V3.0 re-baseline history |

## Design Baseline Notes

- Current implemented baseline is mobile RX/control **plus TX voice** for SunSDR2 DX: mic uplink → Hilbert SSB → 24-bit IQ → `0xFFFD` TX stream, with device DRIVE (`0x0017`) per-band power control (on-air verified: Tune ~12 W, voice 30–40 W PEP).
- Memory-channel backend (`/api/mem_channels`) and per-band power (`/api/band_power`) are implemented and persisted.
- ATR-1000 backend (`/WSATR1000`) and CW/FT8/recording menu targets remain the only outstanding gated hooks. CW/FT8 dead menu links were removed; Recordings remains.
- This SDD should be updated whenever a planned frontend hook gains a real backend implementation or is intentionally removed.

*This document follows IBM Team Solution Design (TeamSD) methodology v2.3.2.*
*Document ID: SDD-SUNMRRC-2026-001*

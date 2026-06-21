# 14. Version History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| SDD V1.0 | 2026-03-15 | MRRC Team | Original MRRC TeamSD document set |
| SDD V2.0 | 2026-05-02 | MRRC Team | MRRC V5.0 mobile UI and broader MRRC capability update |
| SDD V2.1 | 2026-05-10 | MRRC Team | MRRC V5.1 audio preset and Web Audio notes |
| SDD V2.2 | 2026-05-18 | MRRC Team | MRRC V5.2 playback and WDSP notes |
| SDD V3.0 | 2026-06-21 | OpenCode | Re-baselined entire SDD to current `sunmrrc` codebase: FastAPI/Uvicorn, SunSDR2 DX direct UDP, mobile RX/control, HTTPS/WSS, explicit gaps |
| SDD V3.1 | 2026-06-21 | OpenCode | Waterfall rendering (frame accumulation + adaptive noise floor), S-meter exponential smoothing, PING→PONG latency fix, removed duplicate `/WSspectrum` route |

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

- Current implemented baseline is mobile RX/control for SunSDR2 DX.
- TX microphone audio, ATR backend, memory-channel backend, and CW/FT8/recording menu targets are explicitly not claimed as complete.
- This SDD should be updated whenever a planned frontend hook gains a real backend implementation or is intentionally removed.

*This document follows IBM Team Solution Design (TeamSD) methodology v2.3.2.*
*Document ID: SDD-SUNMRRC-2026-001*

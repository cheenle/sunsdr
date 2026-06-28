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
| SDD V3.3 | 2026-06-24 | Claude | Tagged dual-codec audio transport (RX Opus/PCM + TX Opus uplink); corrected `0x1F00` telemetry field offsets (SWR = off16 u16/100, not off26 f32); AD-004 expanded to cover TX path; AD-011 added for SWR field correction; ATT/sample-rate controls documented; TXPLAN phased roadmap marked complete |
| SDD V3.4 | 2026-06-26 | Claude | **TX telemetry field correction**: off30 f32 = forward watts (not off14 cubic fit), off16 u16 = supply volts ×10 (NOT SWR), off18 f32 = PA temp °C. Device has NO reverse-power → no SWR. AD-011 completely rewritten. **TX gain staging (AD-012)**: client preamp 3.0→1.5, server TX_DRIVE_GAIN=3.0, TX_IQ_PEAK=1.0 with tanh soft limiter. 9.5.2 TX signal chain architecture + level-probe diagnostics added. SVG architecture diagrams (system, TX gain staging, telemetry flow). **Ch 15 PTT Safety Architecture**: comprehensive 8-layer defense-in-depth model, full component coverage, SVG diagram. |
| SDD V3.5 | 2026-06-26 | Claude | **Code-sync pass** — reconciled SDD with current source. **Port corrected** to `8889` (server.py default + restart.sh) across all chapters (was `8080`/`8081`). **Bind host** is `[::]` IPv6 dual-stack, not `0.0.0.0`. **`start.sh` removed** — only `restart.sh` exists; ch11 StartScript and ch13 I7 dropped, ch12 startup modes rewritten. **Session auth documented as implemented** (was "no server-side auth"): shared-password `_auth_tokens` + `sunmrrc_auth` cookie + `?token=` WS gating; ch5 NFR-023, ch7, ch11 updated. **Audio transport** reframed as tagged dual-codec (Opus default, Int16 PCM fallback) in the older chapters 2/3/4/5/10 that still said Int16-only. **TX voice** marked High/on-air-verified in ch6 UC-005 and ch13 (was "unresolved"/"not complete"). **Recordings** moved from out-of-scope to implemented (ch3). |
| **SunMRRC V1.0** | **2026-06-24** | **Claude** | **🎉 Initial production release.** RX audio (tagged dual-codec Opus/PCM), TX voice modulation (Hilbert SSB, device DRIVE power control), real-time waterfall, HTTPS/WSS mobile-first, per-band power panel, memory channels, sample-rate selector, WDSP NR2. SDD V3.4 baseline. |

## Key Changes in SDD V3.5

| Chapter | Change |
|---------|--------|
| 1 | Mobile entry port `8080`→`8889` |
| 2 | O2 RX audio availability reworded to tagged dual-codec (Opus default) |
| 3 | RX channel + SC3 reworded to dual-codec; out-of-scope auth narrowed to multi-tenant/per-user (shared-password auth is implemented); recordings moved out of the out-of-scope list |
| 4 | RX/TX audio WebSocket rows + TX voice flow reworded to tagged dual-codec |
| 5 | NFR-004 bandwidth = Opus ~18–24 kbps default / PCM ~256 kbps fallback; NFR-060 decode path covers both codecs; NFR-023 rewritten from "no false claim of auth" to documenting the implemented session auth; port refs fixed |
| 6 | UC-001 entry port fixed; UC-005 boundary line updated — TX voice is implemented and on-air verified |
| 7 | AuthUser deferred-entity reworded — shared-password session auth exists; only per-user identity is deferred |
| 10 | RXAudioService / TXAudioIngressService interface rows reworded to tagged dual-codec |
| 11 | StartScript component row removed (`start.sh` absent); RestartScript description expanded; ops mapping drops `start.sh`; AuthBackend gap reworded to per-user identity; RecordingsPage removed from missing list |
| 12 | Operational model resynced: port `8889`, bind `[::]`, startup modes rewritten around `restart.sh` only, HTTPS log format `https://[::]:<port>`, connection-matrix TX row dual-codec, port-mismatch risk replaced |
| 13 | TX voice feasibility High/verified; I7 (`start.sh`) removed as obsolete; mobile-entry port fixed |
| 14 | Added this V3.5 changelog |
| README | SDD version bumped; runtime-facts source list drops `start.sh`; default entry port `8889` |

## Key Changes in SDD V3.4

| Chapter | Change |
|---------|--------|
| 1 | Executive summary: TX telemetry corrected (off30=W, off16=V, off18=°C, no SWR); TX audio EQ + gain staging added; auth status updated |
| 4 | System context: TX telemetry flow + audio gain flow added to data flows |
| 8 | AD-010 telemetry note corrected; AD-011 completely rewritten (from "SWR off16" to verified field offsets); AD-012 added (TX gain staging) |
| 9 | 9.5 split into RX (9.5.1) and TX (9.5.2) signal chains; TX gain staging table with healthy levels; known gaps updated |
| 11 | Component model: TXModulator, TXAudioEQ, TxCaptureWorklet descriptions updated with gain staging; removed duplicate entries; frontend collaboration expanded |
| 12 | TX verification procedure expanded with level-probe diagnostics and audio quality check |
| 13 | I10 resolved (direct float watts, no fit); I11 added + resolved (gain staging distortion fix) |
| 14 | Added this V3.4 changelog |
| README | Diagrams section added; system at a glance updated; capability summary expanded |
| New | `diagrams/system-architecture.svg`, `diagrams/tx-gain-staging.svg`, `diagrams/telemetry-flow.svg` |
| CLAUDE.md | TX power/drive and telemetry sections match corrected SDD facts |

## Key Changes in SunMRRC V1.0 (SDD V3.3)

| Chapter | Change |
|---------|--------|
| 1 | Updated capability boundaries: device DOES send 0x1F00 during TX; SWR from off16 u16/100 |
| 8 | AD-004 expanded to dual-codec RX+TX; AD-010 telemetry note corrected; AD-011 added (SWR field) |
| 9 | WS table updated for tagged codec; signal processing adds Opus encode step; known gaps updated |
| 11 | Added RxOpusEncoder/TxOpusDecoder components; updated ControlsJS and audio WS descriptions |
| 14 | Added this V3.3 changelog |
| STATUS.md | Complete rewrite — TX implemented, telemetry corrected, codec documented |
| README.md | Features table updated; AD-004 description corrected; WS endpoints updated |
| CLAUDE.md | TX telemetry section rewritten with verified 0x1F00 offsets |
| PROTOCOL.md | 0x1F00 field docs corrected: off16 u16/100 = SWR |
| TXPLAN.md | Marked as completed — all phases implemented |

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

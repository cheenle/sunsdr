# 13. Feasibility Assessment (ART 0530)

## 13.1 Feasibility Summary

| Dimension | Assessment | Explanation |
|-----------|------------|-------------|
| RX technical feasibility | High | IQ receive, demodulation, Int16 broadcast, and browser playback are implemented |
| Control feasibility | High | Frequency, PTT, tune, gains, filters, and DSP commands are implemented |
| Mobile feasibility | Medium-High | HTTPS/WSS solves the key iOS secure-context blocker; device validation remains required |
| TX voice feasibility | High | Mic → Hilbert SSB → 24-bit IQ → `0xFFFD` path implemented and on-air verified (voice 30–40 W PEP) |
| ATR feasibility | Medium | Frontend hooks exist; backend endpoint/proxy work remains |
| Operational feasibility | High | Single-process server and restart script are simple to operate |
| Product completeness | Medium | RX/control baseline is strong; inherited planned menu items need implementation or pruning |

## 13.2 Risks

| ID | Risk | Probability | Impact | Mitigation |
|----|------|-------------|--------|------------|
| R1 | iOS opens HTTP URL instead of HTTPS | Medium | High | Use `https://radio.vlsc.net:8889`; server logs warn on HTTP fallback |
| R2 | Fixed local UDP bind IP differs from deployment host | Medium | High | Parameterize or update `server.py` network constants |
| R3 | TX release command lost or socket half-open | Low-Medium | Critical | ACK retry, backup `s:`, watchdog, forced backend RX handler |
| R4 | WDSP library unavailable | Medium | Medium | Optional initialization; built-in DSP path still runs |
| R5 | TX audio not transmitted despite UI expectations | Resolved | High | TXModulator consumes mic frames → Hilbert SSB → IQ; on-air verified |
| R6 | ATR frontend connection errors confuse user | Medium | Medium | Implement `/WSATR1000` or hide UI until ready |
| R7 | Memory channel manager expects missing API | Resolved | Low-Medium | `/api/mem_channels` implemented (GET/POST + JSON persistence) |
| R10 | Over-driving the PA (no firmware ALC) | Low-Medium | High | Per-band drive caps at the factory-safe ceiling; verify with wattmeter/dummy load before sustained high power |
| R8 | Stale frontend assets | Low | Medium | Service worker bypasses JS/HTML and scripts use version query strings |
| R9 | Certificate expiry | Low | High | Use expiry check script/log and maintain backups |

## 13.3 Assumptions

| ID | Assumption | Confidence | Validation |
|----|------------|------------|------------|
| A1 | SunSDR2 DX is reachable at `192.168.16.200` | Medium | Radio connect log |
| A2 | Host owns local IP `192.168.16.100` | Medium | UDP bind success |
| A3 | Browser supports WebSocket and Web Audio | High | Modern iOS/desktop browsers |
| A4 | TLS certificate/key pair is valid for `radio.vlsc.net` | High | HTTPS request and browser lock |
| A5 | Shared `../web_control` modules remain available | High in current workspace | Import success on startup |
| A6 | Operator reads TX forward power from the ATR-1000 tuner or a wattmeter | High | Device sends `0x1F00` in all modes (273 TX packets verified); on-air values confirmed |

## 13.4 Current Issues

| ID | Issue | Priority | Status | Resolution Path |
|----|-------|----------|--------|-----------------|
| I1 | iPhone HTTP secure-context failure | Critical | Resolved in code | HTTPS/WSS auto-start and frontend WSS selection |
| I2 | TX microphone modulation missing | High | Resolved in code | Server-side mic→Hilbert SSB→IQ path implemented; on-air verified |
| I3 | `/WSATR1000` missing | Medium | Open | Implement backend or disable frontend hooks |
| I4 | `/api/mem_channels` missing | Medium | Resolved in code | GET/POST routes with JSON persistence implemented |
| I5 | Fixed SunSDR local IPs | Medium | Open | Move bind/device stream addresses to env/config |
| I6 | Duplicate `/WSspectrum` route declaration in `server.py` | Low | Resolved in code | Removed the duplicate `ws_spectrum` handler; one declaration remains |
| I7 | `restart.sh` echoes an `http://localhost:<port>` hint even when TLS is active | Low | Cosmetic | The actual scheme is decided by `_find_ssl()` at startup and logged correctly (`https://[::]:<port>`); the script echo is a local-convenience message only. `start.sh` no longer exists. |
| I8 | Control-plane latency stuck at `--ms` (PING dropped before reply) | Medium | Resolved in code | `/WSCTRX` answers `PING` with `PONG` before the colon-based command parse |
| I9 | Low TX output power | High | Resolved in code | DRIVE byte (`0x0017`) was sent in the wrong packet field (payload, not trailing word) → device received drive=0; fixed and per-band power added |
| I10 | TX power formula approximate (cubic fit) | - | Resolved in code | `0x1F00` off30 f32 = forward watts (direct float, no fit needed); verified against real 40m drive sweep + external wattmeter |
| I11 | TX voice audio distorted (tanh over-saturation) | - | Resolved in code | Client preamp 3.0→1.5 (2026-06-25): server input had peak=1.0, drive ×3.0 pushed to 3.95, forcing tanh to squash 75% → heavy distortion. At ×1.5 the tanh barely engages (~4% reduction). |

## 13.5 Dependencies

| ID | Dependency | Type | Status |
|----|------------|------|--------|
| D1 | Python 3.12+ | Runtime | Required |
| D2 | FastAPI/Uvicorn | Runtime | Required |
| D3 | NumPy | Runtime/DSP | Required |
| D4 | Shared `../web_control` modules | Code dependency | Required |
| D5 | SunSDR2 DX network reachability | Hardware/network | Required |
| D6 | TLS cert/key files | Mobile production | Required for iOS secure context |
| D7 | libwdsp | Optional DSP | Optional |
| D8 | Browser Web Audio/WebSocket support | Client | Required |

## 13.6 Feasibility Conclusion

SunMRRC is feasible and already useful as a mobile RX/control baseline for SunSDR2 DX. The primary production-grade path is HTTPS/WSS mobile RX plus direct radio control. Full voice TX, ATR, memory-channel backend, and legacy menu targets should be treated as planned work until implemented and verified.

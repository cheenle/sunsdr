# 5. Non-Functional Requirements (ART 0507)

## 5.1 Performance Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-001 | RX audio latency | Low enough for live monitoring | Critical | Browser listening test and frame timing |
| NFR-002 | Control response | UI command ack within 200 ms on LAN; `PING`->`PONG` round-trip rendered as live ms in status bar | High | WebSocket timestamp/log observation; status-bar latency readout |
| NFR-003 | Spectrum bandwidth | About 512 bytes per spectrum frame at ~38 Hz (~19 KB/s) | Medium | `/WSspectrum` frame size and rate inspection |
| NFR-004 | Audio transport bandwidth | Opus ~18–24 kbps at 16 kHz mono (default); Int16 PCM ~256 kbps fallback | Medium | UI bitrate monitor |
| NFR-005 | CPU stability | No sustained overload from IQ/DSP loop | High | Activity Monitor/top and log observation |
| NFR-006 | Client fan-out | Multiple RX/control clients without server crash | Medium | Multi-browser session test |
| NFR-007 | Waterfall render quality | Client averages ~10 frames per row (38 Hz -> ~3.8 Hz) with adaptive per-row noise floor (30th percentile) so signals stay visible against a stable blue noise background regardless of noise level | Medium | Mobile waterfall observation under varying noise floors |

## 5.2 Availability Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-010 | Restart recovery | `restart.sh` restarts service on configured port | High | Script output and `server.log` |
| NFR-011 | Port cleanup | Only listening process for selected port is killed | High | `restart.sh` behavior |
| NFR-012 | WebSocket reconnect | Frontend retries/handles connection loss where implemented | High | Network interruption test |
| NFR-013 | PTT release safety | Release command has retry/watchdog fallback | Critical | Force closed/half-open control socket test |

## 5.3 Security Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-020 | Secure browser origin | HTTPS/WSS in mobile production | Critical | Load `https://radio.vlsc.net:8889` |
| NFR-021 | TLS material isolation | Key files are referenced, never embedded in docs/code output | Critical | Repository and SDD review |
| NFR-022 | HTTP fallback control | HTTP only when certs missing or `DISABLE_SSL=1` | High | Startup log review |
| NFR-023 | Session authentication | Shared-password session auth gates all routes and WS endpoints (`_auth_tokens`, `sunmrrc_auth` cookie, 30-day validity, `?token=` on WS); unauthenticated visitors redirect to `/login` | High | `server.py` route + `_authed()` review |

## 5.4 Compatibility Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-030 | iOS Safari | RX audio, mic permission, touch controls under HTTPS | Critical | iPhone test |
| NFR-031 | Desktop browser | RX audio/control usable in Chrome/Safari/Firefox | High | Desktop browser test |
| NFR-032 | SunSDR2 DX | Direct UDP protocol remains compatible | Critical | Radio connect and control test |
| NFR-033 | WDSP optionality | App runs when libwdsp is unavailable | High | Startup without `libwdsp` |

## 5.5 Operability Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-040 | Logging | Startup, TLS, radio connect, IQ bind are logged | High | `server.log` |
| NFR-041 | Configuration | `DEVICE_HOST`, `WEB_PORT`, `DISABLE_SSL` environment controls | Medium | Environment-variable startup test |
| NFR-042 | Certificate monitoring | Certificate expiry can be checked with scripts/logs under `certs/` | Medium | `certs/check_ssl_expiry.sh` output |
| NFR-043 | Static cache safety | JS/HTML should not be stale under service worker | High | `static/sw.js` bypasses JS/HTML cache |

## 5.6 Maintainability Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-050 | Small backend surface | WebSocket handlers remain understandable in `server.py` | Medium | Code review |
| NFR-051 | Explicit gaps | Planned UI hooks are documented until implemented or removed | High | SDD review |
| NFR-052 | Shared dependency awareness | Imported `../web_control` modules are treated as design dependencies | High | Import-path review |

## 5.7 Audio Quality Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-060 | RX sample format | Browser RX decodes tagged frames: Opus via WASM `OpusDecoder`, Int16 PCM via `decodeInt16Audio`, both to Float32 | Critical | Codec decode path and listening test |
| NFR-061 | Demodulation quality | USB/LSB/AM/FM/CW modes map to DSP concept where supported | High | Mode change and listening test |
| NFR-062 | WDSP quality | NR2/NB/ANF/NF/AGC available when library loads | Medium | `getWDSPStatus` response |
| NFR-063 | TX audio quality | Mic → SAB ring buffer (no main-thread jitter) → Opus → 300 Hz HPF (4th-order Butterworth, reclaims ~15% PA from sub-300 Hz waste band) → Python Hilbert SSB (sole SSB path, WDSP C-chain removed) → 24-bit IQ with tanh soft-limiter (barely engaged at <4% duty); 96% SSB efficiency, 37 dB SNR, zero audio dropouts; phase-continuous resampler prevents clicks | High | On-air listening report; verified voice 30–40 W PEP; 37 dB SNR measured at far end |
| NFR-064 | S-meter readability | Needle smoothed with asymmetric exponential filter (attack alpha=0.5, release alpha=0.15) so it tracks rising signals fast and decays slowly without per-frame jitter | Medium | Mobile S-meter observation during signal changes |
| NFR-065 | TX output power | Per-band drive (`0x0017`) sets power; square-root taper byte; ALC unsupported in firmware so drive is the sole power lever; IQ peak ~0.69, RMS ~0.68 at 100% drive (TX_DRIVE_GAIN=2.8) | High | Wattmeter / ATR-1000 reading |
| NFR-066 | TX SAB ring buffer performance | AudioWorklet writes float32 directly to SharedArrayBuffer ring; Opus Worker reads from same SAB; zero-copy, zero-main-thread path; COEP credentialless headers required for SAB support; eliminates mid-call dropouts (main-thread jitter was dominant cause) | High | Verified zero dropouts in on-air TX sessions |
| NFR-067 | TX 300 Hz highpass (HPF) audio quality | 4th-order Butterworth HPF at 300 Hz removes sub-300 Hz mic energy (38.7% of total) before SSB modulation; the SSB modulator suppresses this band anyway, so the HPF reclaims headroom for voice-band power without altering voice timbre; reduces tanh limiter engagement and improves SSB efficiency to 96% | High | Pre-modulation spectrum analysis and far-end SNR measurement |

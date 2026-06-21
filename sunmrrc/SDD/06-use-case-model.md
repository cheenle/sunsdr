# 6. Use Case Model (ART 0508)

## 6.1 Actors

| Actor | Description |
|-------|-------------|
| HAM Operator | Primary user controlling SunSDR2 DX from browser |
| System Maintainer | Starts service, manages certificates, observes logs, validates network |
| Browser Runtime | Supplies WebSocket, Web Audio, touch, microphone, and secure-context behavior |
| SunSDR2 DX | Radio device controlled and streamed by UDP |

## 6.2 Core Use Cases

### UC-001: Start Mobile Session

| Field | Description |
|-------|-------------|
| Goal | Open the mobile UI and establish control/RX/spectrum channels |
| Preconditions | Server running, TLS configured for mobile production, browser can reach host |
| Basic Flow | User opens `https://radio.vlsc.net:8889`; server returns `index.html`; scripts load; user powers on; frontend opens `/WSCTRX`, `/WSaudioRX`, `/WSaudioTX`; `Waterfall_start()` opens `/WSspectrum` (and `Waterfall_stop()` closes it on power off) |
| Postconditions | UI displays connection state, frequency/mode, network bitrate/latency when available |
| Exceptions | HTTP on iOS prevents reliable mic/audio permissions; stale service-worker cache must not serve old JS/HTML |

### UC-002: Remote Receive Audio

| Field | Description |
|-------|-------------|
| Goal | Hear demodulated SunSDR RX audio in the browser |
| Preconditions | Radio connected; IQ stream available on UDP `50002`; `/WSaudioRX` connected |
| Basic Flow | Server receives IQ packet; decodes 24-bit I/Q; feeds `StreamProcessor`; obtains PCM; resamples to 16 kHz; broadcasts Int16 bytes; browser decodes to Float32; Web Audio plays output |
| Postconditions | Operator hears RX audio and bitrate monitor reflects traffic |
| Exceptions | If AudioContext is suspended, frontend resume/unlock handling is required; if HTTP on iOS, playback/mic behavior may fail |

### UC-003: Tune Frequency and Mode

| Field | Description |
|-------|-------------|
| Goal | Change radio frequency and DSP demodulation mode |
| Preconditions | `/WSCTRX` connected and radio control initialized |
| Basic Flow | User taps band/step/frequency/mode controls; frontend sends `setFreq:*` or `setMode:*`; server calls `radio.set_frequency()` or demodulator `set_mode()`; server returns `getFreq:*` or `getMode:*` |
| Postconditions | UI reflects new state; radio receives new frequency command |
| Notes | Mode is software DSP-side in this codebase; hardware remains IQ/mode-agnostic for `setMode` |

### UC-004: Monitor Spectrum and S-Meter

| Field | Description |
|-------|-------------|
| Goal | See signal context while listening |
| Preconditions | IQ processing loop is active |
| Basic Flow | Spectrum processor emits FFT (~38 Hz); server derives S estimate and sends `getSignalLevel:*`; server quantizes the 512-bin spectrum to uint8 and broadcasts `/WSspectrum`; browser smooths the S-meter and renders the waterfall |
| Waterfall Rendering | Browser accumulates `WF_DECIMATE=10` frames and averages them into one row (≈38 Hz → ≈3.8 Hz, slower and smoother); per row it derives an adaptive noise floor (the `WF_PCTL=0.30` percentile of that row plus `WF_HEADROOM=2`), subtracts it, adds a blue sea-floor bias (`WF_BIAS=52`) and applies contrast gain (`WF_GAIN=8.0`); the resulting byte indexes a black→blue→cyan→yellow→red colour ramp so noise stays an even blue field and signals rise to yellow/red regardless of absolute noise level |
| S-Meter Smoothing | Browser applies an asymmetric exponential filter in `updateSMeter` — attack α=0.5, release α=0.15 — so the needle jumps up quickly on a new signal and decays slowly, giving a stable, readable reading instead of per-frame jitter |
| Postconditions | Operator sees a stable S-level and a slow, high-contrast waterfall |

### UC-005: PTT and Tune Control

| Field | Description |
|-------|-------------|
| Goal | Key/de-key transmitter safely and support tune command |
| Preconditions | Control WebSocket connected; radio control initialized |
| Basic Flow | User presses PTT or TUNE; frontend sends `setPTT:true` or `tune:true`; server calls radio setter and updates demodulator PTT state; user releases; frontend sends false command and expects ack |
| Postconditions | Radio returns to RX after release |
| Safety Flows | Frontend release ACK retry, backup `s:` command over TX socket, lock-leak detection, PTT watchdog, backend `s` handler forces `set_ptt(False)` |
| Boundary | Voice TX audio modulation is not complete; this is currently keying/control, not full microphone transmission |

### UC-006: Adjust DSP and Audio Settings

| Field | Description |
|-------|-------------|
| Goal | Improve RX intelligibility and adjust gain/filter settings |
| Preconditions | Control WebSocket connected; DSP processor initialized |
| Basic Flow | User toggles WDSP, NR2, NB, ANF, NF, AGC or changes filter/gain; frontend sends `setWDSP*`, `setFilter`, `setAFGain`, `setRFGain`, `setPreamp`, `setAGC`; server updates demodulator/radio and broadcasts state |
| Postconditions | Audio chain and UI state are updated |
| Exceptions | WDSP setters are safe no-ops or report unavailable when `libwdsp` is absent |

## 6.3 Extended and Planned Use Cases

| ID | Use Case | Current State |
|----|----------|---------------|
| UC-007 | Browser microphone TX audio | Frontend capture/EQ/Opus code exists; server modulation path open |
| UC-008 | Memory channel save/recall | Frontend manager exists; backend `/api/mem_channels` implemented |
| UC-009 | ATR-1000 power/SWR | UI placeholders/hooks exist; backend `/WSATR1000` missing |
| UC-010 | CW decoder | Menu link exists; page absent in current repository snapshot |
| UC-011 | FT8 automation | Menu link exists; page absent in current repository snapshot |
| UC-012 | Recordings/logbook | Menu links exist; backend/pages absent in current repository snapshot |

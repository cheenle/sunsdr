# 9. Architecture Overview (ART 0512)

## 9.1 Logical Architecture

```text
Mobile Browser
  index.html / mobile.css / controls.js / mobile.js / modules
  Web Audio playback, mic capture, PTT UI, waterfall canvas
        |
        | HTTPS/WSS
        v
FastAPI SunMRRC App (`server.py`)
  static file catch-all
  /WSCTRX control
  /WSaudioRX RX audio fan-out
  /WSaudioTX TX mic uplink -> SSB modulator -> TX IQ stream
  /WSspectrum waterfall fan-out
        |
        | in-process imports
        v
Shared SunSDR + DSP Modules (`../web_control`)
  SunSDR2DXClient
  StreamProcessor
  SpectrumProcessor
  AudioDemodulator
        |
        | UDP
        v
SunSDR2 DX
```

## 9.2 Runtime View

| Runtime Element | Responsibility |
|-----------------|----------------|
| `app = FastAPI(title="SunMRRC")` | Owns HTTP and WebSocket route registration |
| `startup()` | Connects radio and creates DSP processor, then starts IQ processing task |
| `_process_iq_stream()` | Owns UDP IQ socket, packet loop, PTT dummy stream behavior, DSP feed, audio/spectrum dispatch |
| `_send_ctrl()` | Broadcasts text state updates to control clients |
| `_broadcast_audio()` | Resamples DSP PCM to 16 kHz Int16 and broadcasts RX frames |
| `_broadcast_spectrum()` | Quantizes FFT bins and broadcasts waterfall frames |
| `_find_ssl()` | Selects HTTPS runtime based on local cert/key files |

## 9.3 WebSocket Architecture

| Endpoint | Client File | Server Handler | Payload |
|----------|-------------|----------------|---------|
| `/WSCTRX` | `controls.js`, `mobile.js`, modules | `ws_ctrl()` | Text commands and text responses |
| `/WSaudioRX` | `controls.js` | `ws_audio_rx()` | Binary tagged dual-codec (0x00=PCM / 0x01=Opus) server → client |
| `/WSaudioTX` | `controls.js`, `tx_button.js`, `tx_capture_worklet.js` | `ws_audio_tx()` | Binary tagged mic frames (Opus/PCM) → decode → `TXModulator` → SunSDR TX IQ |
| `/WSspectrum` | `controls.js` | `ws_spectrum()` | Binary uint8 spectrum rows |

## 9.4 Control Command Architecture

| Command Group | Commands | Target |
|---------------|----------|--------|
| Liveness | `PING` -> `PONG` | Control socket |
| Query | `getFreq`, `getMode`, `getPTT`, `getWDSPStatus`, `getWDSPNotches` | Radio/DSP snapshot |
| Radio | `setFreq`, `setPTT`, `tune`, `setAFGain`, `setRFGain`, `setPreamp`, `setAGC`, `setFilter`, `setDrive` | `SunSDR2DXClient` and demodulator |
| DSP | `setMode`, `setWDSPEnabled`, `setWDSPNR2Level`, `setWDSPNR2`, `setWDSPNB`, `setWDSPANF`, `setWDSPNFEnabled`, `setWDSPAGC`, notch commands | `AudioDemodulator` |
| Safety/Misc | `s`, `cq` | Force RX, unblock CQ state machine |
| Codec | `setOpus`, `getOpus` | Toggle RX audio codec between Opus and Int16 PCM |
| ATT | `setATT` | Hardware attenuator: 0=-20dB, 1=-10dB, 2=0dB, 3=+10dB |
| Sample Rate | `setSampleRate` | IQ bandwidth selector: 39k/78k/156k/312k via 0x0001 HW_INIT |

## 9.5 Signal Processing Architecture

### 9.5.1 RX Signal Chain

```text
UDP IQ packet
  -> validate magic/subtype
  -> 24-bit signed I/Q decode
  -> complex64 normalized IQ
  -> StreamProcessor.feed_iq()
  -> SpectrumProcessor FFT -> latest_spectrum
  |    -> percentile -> getSignalLevel:* (control broadcast)
  |    -> _broadcast_spectrum() uint8 quantize -> WSspectrum (when spectrum_clients present)
  -> AudioDemodulator -> PCM audio buffer
  -> server resample to 16 kHz
  -> Opus encode (or PCM passthrough) with 1-byte codec tag
  -> WSaudioRX broadcast
```

### 9.5.2 TX Signal Chain

```text
Browser Mic (48 kHz)
  -> TxCaptureProcessor AudioWorklet (3:1 box-average downsample → 16 kHz)
  -> 20 ms Int16 PCM frames (320 samples, 640 bytes)
```

```text
Browser EQ Pipeline (Web Audio, all phase-continuous):
  micSource → preamp(×1.5, +3.5dB) → antiAlias(4.5kHz, ×2) →
  eqLow(350Hz peaking) → eqMid(1500Hz peaking) → eqHigh(2700Hz highshelf) →
  midCut → presence → compressor(3:1, thr=-24dB, knee=12) →
  makeup(×1.6) → noiseGate → gain_node → AudioWorklet sink
  Presets: DEFAULT(+6/+8/-6dB), MEDIUM(+9/+10/-12dB), STRONG(+12/+12/-18dB), RAGCHEW
```

```text
Server TX Pipeline:
  WSaudioTX Opus/PCM tagged frame
    -> TxOpusDecoder (if Opus) or Int16→float
    -> TXModulator.feed_audio()
    -> continuous fractional resampler (16k → 15625 Hz)
    -> overlap-save Hilbert SSB (USB/LSB, 80-sample hops, 256-sample margin)
    -> linear upsample (15625 → 39063 Hz, ×2.5)
    -> TX_DRIVE_GAIN (×3.0) × drive (0..1)
    -> tanh soft limiter @ TX_IQ_PEAK (1.0)
    -> 24-bit IQ packing (200 samples → 1200 bytes)
    -> jitter buffer (hysteresis: prime 24 pkts, reprime 12 pkts)
    -> TX pacer thread (5.12 ms/pkt, adaptive ±15%)
    -> 0xFFFD UDP to device :50002
```

**TX gain staging** (verified 2026-06-25 against server.log level probes):
| Stage | Peak | RMS | Notes |
|-------|------|-----|-------|
| Client mic input | ~0.15 | ~0.03 | After preamp ×1.5 + EQ, before AudioWorklet Int16 clip |
| Server input (post-decode) | ~0.50 | ~0.05 | Client gain staging keeps headroom below 1.0 |
| After Hilbert SSB | ~0.65 | ~0.07 | Hilbert adds ~30% peak from analytic-phase construction |
| After drive gain (×3.0) | ~1.95 | ~0.21 | Drive lifts into tanh knee |
| After tanh(1.0) | ~0.96 | ~0.19 | tanh engages lightly (~4% peak reduction) — clean SSB |

**Key design point**: The client preamp was reduced from 3.0→1.5 on 2026-06-25. At ×3.0, the server input saturated (peak≈1.0), Hilbert pushed to 1.32, and drive ×3.0 forced peaks to 3.95 — the tanh limiter had to squash 75% of peak amplitude, producing heavy saturation distortion. At ×1.5, the tanh barely engages (~4% reduction), preserving the clean SSB envelope while device drive (0x0017) controls actual RF power.

The browser renders each waterfall row by accumulating ~10 frames (38 Hz -> ~3.8 Hz), subtracting a per-row adaptive noise floor (30th percentile), then biasing the noise to a blue baseline and stretching signal contrast before mapping through a black/blue/cyan/yellow/red colour ramp. The S-meter applies asymmetric exponential smoothing client-side (attack alpha 0.5 / release alpha 0.15) so the needle rises fast and falls slowly.

RX audio is tagged dual-codec: each `/WSaudioRX` binary frame carries a 1-byte prefix (0x00=Int16 PCM, 0x01=Opus 16 kHz mono). The client inspects the tag and decodes accordingly — Opus via WASM `OpusDecoder`, PCM via `decodeInt16Audio()`. Default is Opus (~18-24 kbps); switchable via `setOpus:` / Audio Codec menu. Falls back to PCM if `libopus` is unavailable on the server.

## 9.6 Frontend Architecture

| File | Responsibility |
|------|----------------|
| `index.html` | Mobile UI structure, hidden compatibility DOM, script order |
| `mobile.css` | Mobile visual system, safe areas, controls, panels |
| `controls.js` | Core audio/control WebSockets, RX decode/playback, signal display, `/WSspectrum` waterfall rendering (frame accumulation + adaptive noise floor + colour ramp), legacy compatibility functions |
| `mobile.js` | Mobile-specific state, band/mode UX, `updateSMeter` exponential smoothing, memory manager, menus, DSP panel, ATR frontend hooks |
| `tx_button.js` | Touch/PTT flow, lock handling, watchdog, warm-up frames |
| `modules/ptt_manager.js` | PTT command state, release ACK retry, status display |
| `modules/tune_cq.js` | Tune and CQ UI state machine |
| `modules/tx_audio_eq.js` | Browser TX EQ presets and Web Audio nodes |
| `modules/settings_manager.js` | Cookie-backed preferences and frequency bookmarks |
| `rx_worklet_processor.js` | AudioWorklet queue player for Float32 frames where used |
| `sw.js` | Static asset cache; explicitly bypasses JS/HTML |

## 9.7 Deployment Architecture

```text
restart.sh
  -> find old server.py by cwd
  -> terminate old process
  -> clear listening port only
  -> activate ../venv if present
  -> run python3 server.py
  -> log to server.log

server.py
  -> _find_ssl()
  -> uvicorn.run(... ssl_certfile/keyfile ...) or HTTP fallback
```

## 9.8 Known Architectural Gaps

| Gap | Impact |
|-----|--------|
| Fixed local IQ bind/send IPs in `server.py` | Deployment tied to current LAN topology |
| `/WSATR1000` stub | ATR UI status remains placeholder; accepts connections but doesn't interface with real tuner hardware. Only available SWR source. |
| CW/FT8 pages absent | Menu links removed from the navigation; pages not present in this snapshot |
| Device telemetry: no SWR | `0x1F00` has no reverse-power field (off30=forward W, off16=supply V, off18=PA temp); external tuner required for SWR |

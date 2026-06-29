# 9. Architecture Overview (ART 0512)

## 9.1 Logical Architecture

```text
Mobile Browser
  index.html / mobile.css / controls.js / mobile.js / modules
  Web Audio playback, mic capture, PTT UI, waterfall canvas
        |
        | HTTPS/WSS  (COOP: same-origin / COEP: credentialless / CORP: cross-origin)
        v
FastAPI SunMRRC App (`server.py`)
  static file catch-all
  /WSCTRX control
  /WSaudioRX RX audio fan-out
  /WSaudioTX TX mic uplink -> TxOpusDecoder -> TXModulator -> TX IQ stream
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

### 9.1.1 TX Audio Flow (Browser Side)

```text
Browser Mic (48 kHz)
  → TxCaptureSABProcessor AudioWorklet (3:1 box-average downsample → 16 kHz float32)
  → SharedArrayBuffer ring buffer (lock-free SPSC, 16384 samples ≈ 1.024 s)
  → TX Opus Worker (poll every 3 ms, batch into 20 ms frames)
  → Opus encode (28 kbps CBR, VBR, FEC, DTX)
  → /WSaudioTX binary frames (tagged 0x01 Opus or 0x00 PCM)
```

Zero-main-thread path: the main thread creates the SAB and passes it to both the AudioWorklet (producer) and Opus Worker (consumer), then never touches audio samples again. GC pauses and UI jank cannot stall the TX stream.

### 9.1.2 TX Signal Chain (Server Side)

```text
Server TX Pipeline:
  WSaudioTX Opus/PCM tagged frame
    → TxOpusDecoder (if Opus) or Int16→float
    → TXModulator.feed_audio()
    → continuous DC blocker (one-pole IIR highpass @ 20 Hz, cross-frame continuous)
    → 300 Hz 4th-order Butterworth highpass (removes ~38% sub-voice energy, reallocates PA headroom)
    → anti-alias LPF (4th-order Butterworth @ 3.6 kHz, safety net below 7.8 kHz Nyquist)
    → continuous fractional resampler (16k → 15625 Hz)
    → overlap-save Hilbert SSB (USB/LSB, 80-sample hops, 256-sample margin)
    → linear upsample (15625 → 39063 Hz, ×2.5)
    → TX_DRIVE_GAIN (×2.8) × drive (0..1)
    → tanh soft limiter @ TX_IQ_PEAK (1.0)
    → 24-bit IQ packing (200 samples → 1200 bytes)
    → jitter buffer (hysteresis: prime 24 pkts, reprime 12 pkts)
    → TX pacer thread (5.12 ms/pkt, adaptive ±25%)
    → 0xFFFD UDP to device :50002
```

**TX modulation is Python Hilbert only** — no WDSP TX C-chain. The WDSP library is used exclusively for RX (NR2, AGC, ANF, SNB on the IQ-rate DSP path). The TX path is a pure Python/NumPy/SciPy pipeline with Hilbert analytic-signal SSB generation.

**TX gain staging** (verified 2026-06-25 against server.log level probes):
| Stage | Peak | RMS | Notes |
|-------|------|-----|-------|
| Client mic input | ~0.15 | ~0.03 | After preamp ×1.5 + EQ, before AudioWorklet Int16 clip |
| Server input (post-decode) | ~0.50 | ~0.05 | Client gain staging keeps headroom below 1.0 |
| After 300 Hz HPF | ~0.35 | ~0.04 | ~38% energy removed (sub-voice rumble, proximity effect) |
| After Hilbert SSB | ~0.46 | ~0.05 | Hilbert adds ~30% peak from analytic-phase construction |
| After drive gain (×2.8) | ~1.28 | ~0.14 | Drive lifts into tanh knee (~25% of peaks engage) |
| After tanh(1.0) | ~0.85 | ~0.13 | tanh engages lightly — clean SSB envelope |

**Key design point**: The client preamp was reduced from 3.0→1.5 on 2026-06-25. TX_DRIVE_GAIN was reduced from 3.5→2.8 on 2026-06-29 (3.5 put tanh at ~60% duty cycle on voice peaks, gain-modulating the SSB envelope). At ×2.8, the tanh barely engages (~25% of peaks), preserving the clean SSB envelope while device drive (0x0017) controls actual RF power.

The browser renders each waterfall row by accumulating ~10 frames (38 Hz -> ~3.8 Hz), subtracting a per-row adaptive noise floor (30th percentile), then biasing the noise to a blue baseline and stretching signal contrast before mapping through a black/blue/cyan/yellow/red colour ramp. The S-meter applies asymmetric exponential smoothing client-side (attack alpha 0.5 / release alpha 0.15) so the needle rises fast and falls slowly.

RX audio is tagged dual-codec: each `/WSaudioRX` binary frame carries a 1-byte prefix (0x00=Int16 PCM, 0x01=Opus 16 kHz mono). The client inspects the tag and decodes accordingly — Opus via WASM `OpusDecoder`, PCM via `decodeInt16Audio()`. Default is Opus (~18-24 kbps); switchable via `setOpus:` / Audio Codec menu. Falls back to PCM if `libopus` is unavailable on the server.

## 9.2 Runtime View

| Runtime Element | Responsibility |
|-----------------|----------------|
| `app = FastAPI(title="SunMRRC")` | Owns HTTP and WebSocket route registration |
| `_coop_coep_middleware` | Sets COOP:same-origin / COEP:credentialless / CORP:cross-origin headers required for SharedArrayBuffer |
| `startup()` | Pre-binds IQ socket, starts dedicated keep-alive thread, boots device, creates DSP processor, starts IQ processing task |
| `_iq_keepalive_thread()` | Dedicated OS thread: sends 0xFFFE keep-alive to port 50002 every 0.5s, independent of asyncio event loop — survives blocking WDSP init |
| `_process_iq_stream()` | Owns UDP IQ socket, packet loop, PTT TX stream manageent, DSP feed, audio/spectrum dispatch |
| `_tx_pacer_thread()` | Dedicated OS thread: time.sleep()-paced TX IQ at 5.12 ms/pkt (adaptive ±25%), sends 0xFFFD to device:50002 |
| `_send_ctrl()` | Broadcasts text state updates to control clients |
| `_broadcast_audio()` | Resamples DSP PCM to 48 kHz via Catmull-Rom cubic interpolation, Opus-encodes and broadcasts tagged RX frames |
| `_broadcast_spectrum()` | Quantizes FFT bins and broadcasts waterfall frames |
| `_find_ssl()` | Selects HTTPS runtime based on local cert/key files |

## 9.3 WebSocket Architecture

| Endpoint | Client File | Server Handler | Payload |
|----------|-------------|----------------|---------|
| `/WSCTRX` | `controls.js`, `mobile.js`, modules | `ws_ctrl()` | Text commands and text responses |
| `/WSaudioRX` | `controls.js` | `ws_audio_rx()` | Binary tagged dual-codec (0x00=PCM / 0x01=Opus) server → client |
| `/WSaudioTX` | `tx_opus_worker.js` (SAB path) or `controls.js` (legacy path) | `ws_audio_tx()` | Binary tagged mic frames (Opus/PCM) → decode → `TXModulator` → SunSDR TX IQ |
| `/WSspectrum` | `controls.js` | `ws_spectrum()` | Binary uint8 spectrum rows |

**TX audio WebSocket ownership**: With the SAB ring buffer path, the `/WSaudioTX` WebSocket is owned and managed by the TX Opus Worker thread, not the main thread. The Worker handles connection lifecycle, reconnection, and sending independently. The legacy postMessage path (used when SAB is unavailable) retains the main-thread WebSocket via `controls.js`.

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
  -> server Catmull-Rom cubic resample to 48 kHz
  -> Opus encode (or PCM passthrough) with 1-byte codec tag
  -> WSaudioRX broadcast
```

### 9.5.2 TX Signal Chain

#### Browser Side

```text
Browser Mic (48 kHz)
  -> TxCaptureSABProcessor AudioWorklet (3:1 box-average downsample → 16 kHz float32)
  -> SharedArrayBuffer ring buffer (lock-free SPSC, word[0]=write_pos, word[1]=read_pos, word[2+]=float32 data, 16384 samples)
  -> TX Opus Worker (poll every 3 ms, batch into 20 ms frames)
  -> Opus encode (28 kbps CBR, VBR, FEC, DTX)
  -> /WSaudioTX binary frames (tagged 0x01 Opus or 0x00 PCM)
```

**Zero-main-thread path**: After the SAB is created and passed to the AudioWorklet (producer) and Opus Worker (consumer), the main thread never touches audio samples. The Opus Worker owns its own WebSocket connection to `/WSaudioTX` — it manages connection, reconnection, encoding, and sending independently. This eliminates audio dropouts caused by main-thread GC pauses and UI jank.

**Legacy fallback path** (SAB unavailable / older browsers):
```
AudioWorklet -> postMessage Int16 frames -> main thread -> wsAudioTX.send()
```

#### Browser EQ Pipeline (Web Audio, all phase-continuous)

```text
micSource → preamp(×1.5, +3.5dB) → antiAlias(4.5kHz, ×2) →
eqLow(350Hz peaking) → eqMid(1500Hz peaking) → eqHigh(2700Hz highshelf) →
midCut → presence → compressor(3:1, thr=-24dB, knee=12) →
makeup(×1.6) → noiseGate → gain_node → AudioWorklet sink
Presets: DEFAULT(+6/+8/-6dB), MEDIUM(+9/+10/-12dB), STRONG(+12/+12/-18dB), RAGCHEW
```

#### Server Side

```text
Server TX Pipeline:
  WSaudioTX Opus/PCM tagged frame
    -> TxOpusDecoder (if Opus) or Int16→float
    -> TXModulator.feed_audio()
    -> continuous DC blocker (one-pole IIR highpass @ 20 Hz, cross-frame continuous state)
    -> 300 Hz 4th-order Butterworth highpass (reclaims ~38% PA headroom from sub-voice energy)
    -> anti-alias LPF (4th-order Butterworth @ 3.6 kHz)
    -> continuous fractional resampler (16k → 15625 Hz)
    -> overlap-save Python Hilbert SSB (USB/LSB, 80-sample hops, 256-sample margin)
    -> linear upsample (15625 → 39063 Hz, ×2.5)
    -> TX_DRIVE_GAIN (×2.8) × drive (0..1)
    -> tanh soft limiter @ TX_IQ_PEAK (1.0)
    -> 24-bit IQ packing (200 samples → 1200 bytes, vectorized numpy)
    -> jitter buffer (hysteresis: prime 60 pkts, reprime 20 pkts)
    -> TX pacer thread (5.12 ms/pkt, adaptive ±25%)
    -> 0xFFFD UDP to device :50002
```

**TX modulation properties:**
- Python Hilbert (scipy.signal.hilbert) is the sole SSB modulation path — no WDSP TX C-chain
- Continuous DC blocker: one-pole IIR H(z) = (1 - z^-1) / (1 - R*z^-1), R = exp(-2π*20/16000), persistent state across feed_audio() calls. Replaces per-frame x-=x.mean() which caused DC jumps at 76% of frame boundaries (audible clicks).
- 300 Hz HPF: 4th-order Butterworth (sosfilt, phase-continuous). Removes room rumble, proximity effect, subsonic noise — ~38% of mic energy sits below 300 Hz where SSB suppresses it, wasting PA headroom.
- tanh soft limiter: smooth saturation curve replaces hard magnitude clipping (which generated wideband splatter at syllable boundaries). Ceiling is fixed at TX_IQ_PEAK=1.0, independent of drive — drive scales the level under a constant clip point.
- TX_DRIVE_GAIN=2.8: keeps tanh engagement at ~25% of voice peaks (down from 60% at ×3.5, which gain-modulated the SSB envelope).

**TX gain staging** (verified against server.log level probes):
| Stage | Peak | RMS | Notes |
|-------|------|-----|-------|
| Client mic input | ~0.15 | ~0.03 | After preamp ×1.5 + EQ, before AudioWorklet |
| Server input (post-decode) | ~0.50 | ~0.05 | Client gain staging keeps headroom below 1.0 |
| After 300 Hz HPF | ~0.35 | ~0.04 | ~38% energy removed (sub-voice rumble) |
| After Hilbert SSB | ~0.46 | ~0.05 | Hilbert adds ~30% peak |
| After drive gain (×2.8) | ~1.28 | ~0.14 | Drive lifts into tanh knee |
| After tanh(1.0) | ~0.85 | ~0.13 | tanh engages ~25% — clean SSB |

## 9.6 Frontend Architecture

| File | Responsibility |
|------|----------------|
| `index.html` | Mobile UI structure, hidden compatibility DOM, script order |
| `mobile.css` | Mobile visual system, safe areas, controls, panels |
| `controls.js` | Core audio/control WebSockets, RX decode/playback, signal display, `/WSspectrum` waterfall rendering (frame accumulation + adaptive noise floor + colour ramp), SAB ring buffer creation + TX Worker/Worklet orchestration |
| `mobile.js` | Mobile-specific state, band/mode UX, `updateSMeter` exponential smoothing, memory manager, menus, DSP panel, ATR frontend hooks |
| `tx_button.js` | Touch/PTT flow, lock handling, watchdog, warm-up frames |
| `tx_capture_worklet.js` | AudioWorklet TX capture: 48k→16k box-average downsample, float32 → SAB ring buffer write (zero-main-thread path) or Int16 → postMessage (legacy fallback) |
| `tx_opus_worker.js` | Dedicated Worker thread: SAB ring buffer consumer, Opus encoder, owns `/WSaudioTX` WebSocket, polls SAB every 3 ms, encodes 20 ms frames |
| `modules/ptt_manager.js` | PTT command state, release ACK retry, status display |
| `modules/tune_cq.js` | Tune and CQ UI state machine |
| `modules/tx_audio_eq.js` | Browser TX EQ presets (DEFAULT/MEDIUM/STRONG/RAGCHEW), preamp ×1.5, compressor 3:1, anti-alias 4.5 kHz |
| `modules/settings_manager.js` | Cookie-backed preferences and frequency bookmarks |
| `modules/opus_codec.js` | Browser-side WASM Opus encoder/decoder |
| `modules/opus_wasm.js` | Opus WASM binary loader |
| `rx_worklet_processor.js` | AudioWorklet queue player for Float32 RX frames |
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
| SAB requires COOP/COEP | SharedArrayBuffer needs COOP:same-origin + COEP:credentialless headers; without them, falls back to legacy postMessage path with reduced TX reliability |

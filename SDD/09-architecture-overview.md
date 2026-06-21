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
  /WSaudioTX TX audio transport placeholder
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
| `/WSaudioRX` | `controls.js` | `ws_audio_rx()` | Binary Int16 PCM server -> client |
| `/WSaudioTX` | `controls.js`, `tx_button.js` | `ws_audio_tx()` | Binary/text accepted but not processed into TX modulation |
| `/WSspectrum` | `controls.js` | `ws_spectrum()` | Binary uint8 spectrum rows |

## 9.4 Control Command Architecture

| Command Group | Commands | Target |
|---------------|----------|--------|
| Liveness | `PING` -> `PONG` | Control socket |
| Query | `getFreq`, `getMode`, `getPTT`, `getWDSPStatus`, `getWDSPNotches` | Radio/DSP snapshot |
| Radio | `setFreq`, `setPTT`, `tune`, `setAFGain`, `setRFGain`, `setPreamp`, `setAGC`, `setFilter` | `SunSDR2DXClient` and demodulator |
| DSP | `setMode`, `setWDSPEnabled`, `setWDSPNR2Level`, `setWDSPNR2`, `setWDSPNB`, `setWDSPANF`, `setWDSPNFEnabled`, `setWDSPAGC`, notch commands | `AudioDemodulator` |
| Safety/Misc | `s`, `cq` | Force RX, unblock CQ state machine |

## 9.5 Signal Processing Architecture

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
  -> WSaudioRX broadcast
```

The browser renders each waterfall row by accumulating ~10 frames (38 Hz -> ~3.8 Hz), subtracting a per-row adaptive noise floor (30th percentile), then biasing the noise to a blue baseline and stretching signal contrast before mapping through a black/blue/cyan/yellow/red colour ramp. The S-meter applies asymmetric exponential smoothing client-side (attack alpha 0.5 / release alpha 0.15) so the needle rises fast and falls slowly.

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
| TX audio frames are not consumed by backend | PTT does not equal complete voice transmission |
| Fixed local IQ bind/send IPs in `server.py` | Deployment tied to current LAN topology |
| `/api/mem_channels` absent | Memory manager falls back to cache/offline behavior |
| `/WSATR1000` absent | ATR UI status remains placeholder/failing connection |
| CW/FT8/recording pages absent | Menu links are not complete product flows |

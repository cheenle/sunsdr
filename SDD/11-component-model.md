# 11. Component Model (ART 0515)

## 11.1 Component Inventory

| Component | Type | File | Responsibility |
|-----------|------|------|----------------|
| FastAPIApp | Backend core | `server.py` | Route registration, startup, COOP/COEP middleware, static serving, WebSockets |
| COOPCOEPMiddleware | Backend support | `server.py` | Sets COOP:same-origin / COEP:credentialless / CORP:cross-origin headers required for SharedArrayBuffer |
| RadioClient | Shared backend | `../web_control/sunsdr_direct.py` | SunSDR2 DX UDP protocol, boot sequence, state setters |
| IQLoop | Backend core | `server.py` | UDP IQ socket (pre-bound before boot), packet validation, PTT stream handling, DSP feed |
| IQKeepAliveThread | Backend core | `server.py` | Dedicated OS thread: sends 0xFFFE keep-alive every 0.5s to port 50002, independent of asyncio event loop |
| StreamProcessor | Shared DSP | `../web_control/dsp.py` | Coordinates spectrum and audio demodulation |
| SpectrumProcessor | Shared DSP | `../web_control/dsp.py` | FFT accumulation and dB spectrum generation |
| AudioDemodulator | Shared DSP | `../web_control/dsp.py` | SSB/AM/FM demodulation, filter/volume/PTT, optional WDSP (IQ-rate path for NR2/AI/SNB) |
| WDSPProcessor | Shared DSP | `../web_control/wdsp_wrapper.py` | Optional libwdsp integration — RX only (audio-rate NR2/AGC/NB/ANF fallback) |
| WDSPIQProcessor | Shared DSP | `../web_control/wdsp_wrapper.py` | Optional libwdsp integration — RX only (IQ-rate NR2/ANF/SNB primary path) |
| StaticServer | Backend support | `server.py` | Serves files from `static/` with MIME map |
| ControlWebSocket | Backend core | `server.py` | `/WSCTRX` command loop |
| RXAudioWebSocket | Backend core | `server.py` | `/WSaudioRX` tagged dual-codec fan-out (Opus/PCM) |
| TXAudioWebSocket | Backend core | `server.py` | `/WSaudioTX` tagged mic-frame ingress → codec decode → `TXModulator.feed_audio()` |
| RxOpusEncoder | Shared DSP | `../web_control/opus_rx.py` | Server-side Opus encode for RX broadcast; direct ctypes libopus |
| TxOpusDecoder | Shared DSP | `../web_control/opus_rx.py` | Server-side Opus decode for TX mic uplink |
| TXModulator | Shared DSP | `../web_control/dsp.py` | Mic PCM → continuous DC blocker (20 Hz IIR, cross-frame state) → 300 Hz 4th-order Butterworth HPF → anti-alias LPF (3.6 kHz) → fractional resampler (16k→15625 Hz) → overlap-save Python Hilbert SSB (sole modulation path) → drive gain (×2.8) → tanh soft limiter @ TX_IQ_PEAK (1.0) → 24-bit IQ packing → jitter-buffered 0xFFFD packet queue. See AD-012 for gain staging. |
| TXPacer | Backend core | `server.py` | Dedicated OS thread: drains queued TX IQ at 5.12 ms/pkt (adaptive ±25% pacing); sends `0xFFFD` to device:50002 |
| BandPowerAPI | Backend core | `server.py` | `/api/band_power` GET/POST; persists to `band_power.json`; applies per-band DRIVE via sunsdr_direct |
| SpectrumWebSocket | Backend core | `server.py` | `/WSspectrum` binary fan-out |
| TLSLocator | Backend support | `server.py` | Cert/key discovery and HTTP fallback |
| MobileHTML | Frontend core | `static/index.html` | UI structure and script composition |
| MobileStyles | Frontend core | `static/mobile.css` | Mobile layout and visual controls |
| ControlsJS | Frontend core | `static/controls.js` | WebSocket setup, tagged RX audio decode (Opus/PCM), SAB ring buffer creation, TX Worker/Worklet orchestration, control handlers, waterfall rendering |
| MobileJS | Frontend core | `static/mobile.js` | Mobile UX, state, menus, DSP panel, frontend service hooks |
| SABRingBuffer | Frontend audio | `controls.js` (creation), `tx_capture_worklet.js` (producer), `tx_opus_worker.js` (consumer) | 16384-sample float32 SharedArrayBuffer ring: word[0]=write_pos (Uint32), word[1]=read_pos (Uint32), word[2+]=sample data. Lock-free SPSC via Atomics — AudioWorklet writes, Opus Worker reads, main thread never touches audio samples |
| PTTManager | Frontend safety | `static/modules/ptt_manager.js` | PTT state, release ack retry, status sync |
| TXButton | Frontend safety | `static/tx_button.js` | Touch PTT flow, lock handling, watchdog, warm-up frames |
| TuneCQ | Frontend UX | `static/modules/tune_cq.js` | Tune/CQ button state and commands |
| TXAudioEQ | Frontend audio | `static/modules/tx_audio_eq.js` | TX EQ presets (DEFAULT/MEDIUM/STRONG/RAGCHEW), preamp ×1.5, compressor 3:1, anti-alias lowpass 4.5 kHz. Gain-staged for clean SSB envelope (see AD-012). |
| SettingsManager | Frontend support | `static/modules/settings_manager.js` | Cookie preferences and saved frequency helpers |
| OpusCodec | Frontend audio | `static/modules/opus_codec.js`, `opus_wasm.js` | Browser-side WASM Opus encoder/decoder |
| RxWorklet | Frontend audio | `static/rx_worklet_processor.js` | Queue-based AudioWorklet playback processor |
| TxCaptureWorklet | Frontend audio | `static/tx_capture_worklet.js` | AudioWorklet mic capture: 48k→16k box-average downsample, float32 samples → SAB ring buffer write (zero-main-thread path) or Int16 → postMessage (legacy fallback). Dedicated audio-thread, no main-thread involvement when SAB is active. |
| TxOpusWorker | Frontend audio | `static/tx_opus_worker.js` | Dedicated Worker thread: SAB ring buffer consumer (poll every 3 ms), Opus encoder (28 kbps CBR), owns its own `/WSaudioTX` WebSocket connection. Reads float32 samples from SAB, encodes 20 ms frames, sends tagged binary frames directly. Manages its own WS reconnect lifecycle. |
| ServiceWorker | Frontend support | `static/sw.js` | Static asset cache with JS/HTML bypass |
| RestartScript | Operations | `restart.sh` | Safe restart, cwd-matched process kill, port cleanup, foreground (`-f`) or background launch, log redirection |

## 11.2 Backend Component Collaboration

```text
FastAPIApp.startup()
  -> IQ socket pre-bound (port 50002, before device boot)
  -> IQKeepAliveThread started (dedicated OS thread, survives blocking WDSP init)
  -> RadioClient.connect()
  -> StreamProcessor(SpectrumProcessor, AudioDemodulator)
  -> create_task(_heartbeat_task) — 0x0018 to port 50001 every 0.5s
  -> create_task(IQLoop)

IQLoop (RX)
  -> decode IQ payload
  -> StreamProcessor.feed_iq()
  -> ControlWebSocket broadcast S-meter
  -> SpectrumWebSocket broadcast FFT rows
  -> RXAudioWebSocket broadcast tagged audio (Opus/PCM)

IQLoop (TX — PTT active)
  -> TXPacer thread started (dedicated OS thread, 5.12 ms adaptive pacing)
  -> TXModulator.reset_mic() + reset_tx_ramp()
  -> TX keep-alive (0xFFFE) sent during TX alongside pacer
  -> Telemetry (0x1F00) processed during TX

ControlWebSocket
  -> RadioClient setters
  -> AudioDemodulator setters
  -> ControlWebSocket broadcasts
```

## 11.3 Frontend Component Collaboration

### 11.3.1 TX Audio Flow (SAB Path — Primary)

```text
controls.js (setup only)
  -> Creates SharedArrayBuffer ring (16384 samples, ~1.024 s)
  -> Creates TxOpusWorker (Worker thread)
  -> Loads TxCaptureWorklet (AudioWorklet on audio thread)

TxCaptureWorklet (audio thread — producer)
  -> Mic input: 48 kHz → box-average downsample → 16 kHz float32
  -> SAB ring write: lock-free Atomics.store write_pos
  -> Zero main-thread involvement

TxOpusWorker (Worker thread — consumer)
  -> SAB ring read: poll every 3 ms, Atomics.load write_pos/read_pos
  -> Batch 320 samples (20 ms) per frame
  -> Opus encode (28 kbps CBR, OpusEncoder WASM)
  -> Owns /WSaudioTX WebSocket connection
  -> Sends tagged binary frames (0x01 Opus / 0x00 PCM)
  -> Manages reconnect lifecycle independently

Server (dsp.py TXModulator)
  -> TxOpusDecoder (if Opus) or Int16→float
  -> Continuous DC blocker → 300 Hz HPF → anti-alias LPF
  -> Fractional resampler → Python Hilbert SSB
  -> Drive gain (×2.8) → tanh limiter → 24-bit IQ packing
  -> Jitter-buffered queue → TXPacer → 0xFFFD to device
```

### 11.3.2 TX Audio Flow (Legacy postMessage Path — Fallback)

```text
TxCaptureWorklet (audio thread)
  -> 48k→16k downsample, 20ms Int16 frame accumulation
  -> postMessage Int16 frame to main thread

controls.js (main thread)
  -> onWorkletFrame() receives Int16 frame
  -> wsAudioTX.send() binary frame to /WSaudioTX

Legacy path used when SharedArrayBuffer is unavailable (no COOP/COEP headers,
older browsers, or iOS <15.2).
```

### 11.3.3 RX Audio + UI

```text
controls.js RX
  -> WSaudioRX receives tagged dual-codec frames
  -> tag byte 0x00=PCM → decodeInt16Audio(), 0x01=Opus → WASM OpusDecoder
  -> AudioContext/Worklet or ScriptProcessor playback
  -> WSspectrum receives uint8 rows
  -> Waterfall_start/stop accumulates WF_DECIMATE frames -> adaptive noise floor (WF_PCTL) -> WF_BIAS/WF_GAIN contrast -> canvas row
  -> updateSMeter applies asymmetric exponential smoothing (attack 0.5 / release 0.15)
  -> S-meter/waterfall/status UI updates
```

## 11.4 File-to-Capability Mapping

| Capability | Primary Files |
|------------|---------------|
| HTTPS startup + COOP/COEP | `server.py`, `certs/fullchain.pem`, `certs/radio.vlsc.net.key` |
| Static UI | `static/index.html`, `static/mobile.css`, `static/mobile.js` |
| RX audio | `server.py`, `../web_control/dsp.py`, `static/controls.js` |
| Spectrum | `server.py`, `../web_control/dsp.py`, `static/controls.js` (server quantizes; `controls.js` accumulates/contrast-stretches the waterfall) |
| Frequency/mode/control | `server.py`, `../web_control/sunsdr_direct.py`, `static/controls.js`, `static/mobile.js` |
| PTT safety | `server.py`, `static/modules/ptt_manager.js`, `static/tx_button.js` |
| TX voice modulation | `server.py`, `../web_control/dsp.py` (`TXModulator`, Python Hilbert SSB), `static/tx_capture_worklet.js` (AudioWorklet), `static/tx_opus_worker.js` (Opus Worker + WS), `static/controls.js` (SAB creation + orchestration) |
| TX SAB ring buffer | `static/controls.js` (creation), `static/tx_capture_worklet.js` (producer write), `static/tx_opus_worker.js` (consumer read) |
| TX power / per-band drive | `server.py` (`/api/band_power`), `../web_control/sunsdr_direct.py` (`0x0017`), `static/mobile.js` (Band Power panel), `band_power.json` |
| WDSP controls (RX only) | `server.py`, `../web_control/dsp.py`, `../web_control/wdsp_wrapper.py`, `static/mobile.js` |
| Browser TX EQ | `static/modules/tx_audio_eq.js`, `static/controls.js` |
| TX gain staging | AD-012, `../web_control/dsp.py` (`TXModulator`, `TX_DRIVE_GAIN`, `TX_IQ_PEAK`, 300 Hz HPF, DC blocker), `static/modules/tx_audio_eq.js` (`AudioTX_preamp`) |
| Restart/ops | `restart.sh`, `server.log` |

## 11.5 Component Gaps

| Missing/Incomplete Component | Expected Responsibility |
|------------------------------|-------------------------|
| ATRWebSocketHandler | Implement `/WSATR1000` for power/SWR and tuner control |
| CWPage/FT8Page | Provide targets currently linked by menu (recordings page is implemented) |
| AuthBackend | Upgrade the shared-password session-token auth to per-user server-side identity if multi-user is required |

## 11.6 Removed Components

| Component | Reason |
|-----------|--------|
| WDSPTXProcessor (WDSP TX C-chain) | Removed. TX modulation is exclusively Python Hilbert SSB — no WDSP processing in the TX path. WDSP is RX-only (IQ-rate NR2/ANF/SNB + audio-rate fallback). The TX C-chain was never functional in this codebase and added unnecessary complexity to the architecture. |

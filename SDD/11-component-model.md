# 11. Component Model (ART 0515)

## 11.1 Component Inventory

| Component | Type | File | Responsibility |
|-----------|------|------|----------------|
| FastAPIApp | Backend core | `server.py` | Route registration, startup, static serving, WebSockets |
| RadioClient | Shared backend | `../web_control/sunsdr_direct.py` | SunSDR2 DX UDP protocol, boot sequence, state setters |
| IQLoop | Backend core | `server.py` | UDP IQ socket, packet validation, PTT stream handling, DSP feed |
| StreamProcessor | Shared DSP | `../web_control/dsp.py` | Coordinates spectrum and audio demodulation |
| SpectrumProcessor | Shared DSP | `../web_control/dsp.py` | FFT accumulation and dB spectrum generation |
| AudioDemodulator | Shared DSP | `../web_control/dsp.py` | SSB/AM/FM demodulation, filter/volume/PTT, optional WDSP |
| WDSPWrapper | Shared DSP | `../web_control/wdsp_wrapper.py` | Optional libwdsp integration |
| StaticServer | Backend support | `server.py` | Serves files from `static/` with MIME map |
| ControlWebSocket | Backend core | `server.py` | `/WSCTRX` command loop |
| RXAudioWebSocket | Backend core | `server.py` | `/WSaudioRX` tagged dual-codec fan-out (Opus/PCM) |
| TXAudioWebSocket | Backend core | `server.py` | `/WSaudioTX` tagged mic-frame ingress → codec decode → `TXModulator.feed_audio()` |
| RxOpusEncoder | Shared DSP | `../web_control/opus_rx.py` | Server-side Opus encode for RX broadcast; direct ctypes libopus |
| TxOpusDecoder | Shared DSP | `../web_control/opus_rx.py` | Server-side Opus decode for TX mic uplink |
| TXModulator | Shared DSP | `../web_control/dsp.py` | Mic PCM → fractional resampler → overlap-save Hilbert SSB → drive gain (×3.0) → tanh soft limiter @ TX_IQ_PEAK (1.0) → 24-bit IQ packing → jitter-buffered 0xFFFD packet queue. See AD-012 for gain staging. |
| TXPacer | Backend core | `server.py` | Drains queued TX IQ at 5.12 ms/pkt (adaptive ±15% pacing); sends `0xFFFD` to device:50002 |
| BandPowerAPI | Backend core | `server.py` | `/api/band_power` GET/POST; persists to `band_power.json`; applies per-band DRIVE via sunsdr_direct |
| SpectrumWebSocket | Backend core | `server.py` | `/WSspectrum` binary fan-out |
| TLSLocator | Backend support | `server.py` | Cert/key discovery and HTTP fallback |
| MobileHTML | Frontend core | `static/index.html` | UI structure and script composition |
| MobileStyles | Frontend core | `static/mobile.css` | Mobile layout and visual controls |
| ControlsJS | Frontend core | `static/controls.js` | WebSocket setup, tagged RX audio decode (Opus/PCM), TX audio encode + tag, control handlers, waterfall rendering |
| MobileJS | Frontend core | `static/mobile.js` | Mobile UX, state, menus, DSP panel, frontend service hooks |
| PTTManager | Frontend safety | `static/modules/ptt_manager.js` | PTT state, release ack retry, status sync |
| TXButton | Frontend safety | `static/tx_button.js` | Touch PTT flow, lock handling, watchdog, warm-up frames |
| TuneCQ | Frontend UX | `static/modules/tune_cq.js` | Tune/CQ button state and commands |
| TXAudioEQ | Frontend audio | `static/modules/tx_audio_eq.js` | TX EQ presets (DEFAULT/MEDIUM/STRONG/RAGCHEW), preamp ×1.5, compressor 3:1, anti-alias lowpass 4.5 kHz. Gain-staged for clean SSB envelope (see AD-012). |
| SettingsManager | Frontend support | `static/modules/settings_manager.js` | Cookie preferences and saved frequency helpers |
| OpusCodec | Frontend audio | `static/modules/opus_codec.js`, `opus_wasm.js` | Browser-side WASM Opus encoder/decoder for TX pipeline (28 kbps, VBR, FEC, DTX) |
| RxWorklet | Frontend audio | `static/rx_worklet_processor.js` | Queue-based AudioWorklet playback processor |
| TxCaptureWorklet | Frontend audio | `static/tx_capture_worklet.js` | Dedicated-thread mic capture AudioWorklet: 48k→16k box-average downsample, 20ms Int16 frame output, ScriptProcessor fallback |
| ServiceWorker | Frontend support | `static/sw.js` | Static asset cache with JS/HTML bypass |
| RestartScript | Operations | `restart.sh` | Safe restart, cwd-matched process kill, port cleanup, foreground (`-f`) or background launch, log redirection |

## 11.2 Backend Component Collaboration

```text
FastAPIApp.startup()
  -> RadioClient.connect()
  -> StreamProcessor(SpectrumProcessor, AudioDemodulator)
  -> create_task(IQLoop)

IQLoop
  -> decode IQ payload
  -> StreamProcessor.feed_iq()
  -> ControlWebSocket broadcast S-meter
  -> SpectrumWebSocket broadcast FFT rows
  -> RXAudioWebSocket broadcast PCM

ControlWebSocket
  -> RadioClient setters
  -> AudioDemodulator setters
  -> ControlWebSocket broadcasts
```

## 11.3 Frontend Component Collaboration

```text
index.html
  -> controls.js initializes power/control/audio sockets
  -> modules provide PTT, settings, TX EQ, tune/CQ
  -> mobile.js binds mobile-specific UI and DSP panel
  -> tx_button.js owns touch PTT behavior

controls.js + tx_audio_eq.js + tx_capture_worklet.js
  -> MediaHandler.callback() builds the Web Audio TX EQ chain:
     micSource → preamp(×1.5) → antiAlias(×2) → eqLow → eqMid → eqHigh →
     midCut → presence → compressor(3:1) → makeup(×1.6) → noiseGate → gain_node →
     AudioWorklet (tx-capture) or ScriptProcessor
  -> AudioWorklet downsamples 48k→16k, accumulates 20ms Int16 frames
  -> OpusEncoderProcessor.onWorkletFrame() tags+encodes (Opus 0x01 or PCM 0x00)
  -> wsAudioTX.send() binary frames

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
| HTTPS startup | `server.py`, `certs/fullchain.pem`, `certs/radio.vlsc.net.key` |
| Static UI | `static/index.html`, `static/mobile.css`, `static/mobile.js` |
| RX audio | `server.py`, `../web_control/dsp.py`, `static/controls.js` |
| Spectrum | `server.py`, `../web_control/dsp.py`, `static/controls.js` (server quantizes; `controls.js` accumulates/contrast-stretches the waterfall) |
| Frequency/mode/control | `server.py`, `../web_control/sunsdr_direct.py`, `static/controls.js`, `static/mobile.js` |
| PTT safety | `server.py`, `static/modules/ptt_manager.js`, `static/tx_button.js` |
| TX voice modulation | `server.py`, `../web_control/dsp.py` (`TXModulator`), `static/controls.js`, `static/tx_capture_worklet.js` |
| TX power / per-band drive | `server.py` (`/api/band_power`), `../web_control/sunsdr_direct.py` (`0x0017`), `static/mobile.js` (Band Power panel), `band_power.json` |
| WDSP controls | `server.py`, `../web_control/dsp.py`, `../web_control/wdsp_wrapper.py`, `static/mobile.js` |
| Browser TX EQ | `static/modules/tx_audio_eq.js`, `static/controls.js`, `static/tx_capture_worklet.js` |
| TX gain staging | AD-012, `../web_control/dsp.py` (`TXModulator`, `TX_DRIVE_GAIN`, `TX_IQ_PEAK`), `static/modules/tx_audio_eq.js` (`AudioTX_preamp`) |
| Restart/ops | `restart.sh`, `server.log` |

## 11.5 Component Gaps

| Missing/Incomplete Component | Expected Responsibility |
|------------------------------|-------------------------|
| ATRWebSocketHandler | Implement `/WSATR1000` for power/SWR and tuner control |
| CWPage/FT8Page | Provide targets currently linked by menu (recordings page is implemented) |
| AuthBackend | Upgrade the shared-password session-token auth to per-user server-side identity if multi-user is required |

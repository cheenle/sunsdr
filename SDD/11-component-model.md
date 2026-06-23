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
| RXAudioWebSocket | Backend core | `server.py` | `/WSaudioRX` client set and binary fan-out |
| TXAudioWebSocket | Backend core | `server.py` | `/WSaudioTX` mic-frame ingress → `TXModulator.feed_audio()` |
| TXModulator | Shared DSP | `../web_control/dsp.py` | Mic PCM → fractional resample → Hilbert SSB → 24-bit IQ → `0xFFFD` TX packets |
| TXPacer | Backend core | `server.py` | Drains queued TX IQ at 5.12 ms/pkt; sends `0xFFFD` to device:50002 |
| BandPowerAPI | Backend core | `server.py` | `/api/band_power` GET/POST; persists to `band_power.json`; applies per-band DRIVE |
| SpectrumWebSocket | Backend core | `server.py` | `/WSspectrum` binary fan-out |
| TLSLocator | Backend support | `server.py` | Cert/key discovery and HTTP fallback |
| MobileHTML | Frontend core | `static/index.html` | UI structure and script composition |
| MobileStyles | Frontend core | `static/mobile.css` | Mobile layout and visual controls |
| ControlsJS | Frontend core | `static/controls.js` | WebSocket setup, RX audio decode/playback, control handlers |
| MobileJS | Frontend core | `static/mobile.js` | Mobile UX, state, menus, DSP panel, frontend service hooks |
| PTTManager | Frontend safety | `static/modules/ptt_manager.js` | PTT state, release ack retry, status sync |
| TXButton | Frontend safety | `static/tx_button.js` | Touch PTT flow, lock handling, watchdog, warm-up frames |
| TuneCQ | Frontend UX | `static/modules/tune_cq.js` | Tune/CQ button state and commands |
| TXAudioEQ | Frontend audio | `static/modules/tx_audio_eq.js` | TX EQ presets and Web Audio nodes |
| SettingsManager | Frontend support | `static/modules/settings_manager.js` | Cookie preferences and saved frequency helpers |
| OpusCodec | Frontend audio | `static/modules/opus_codec.js`, `opus_wasm.js` | Browser-side Opus support for TX pipeline |
| RxWorklet | Frontend audio | `static/rx_worklet_processor.js` | Queue-based AudioWorklet playback processor |
| TxCaptureWorklet | Frontend audio | `static/tx_capture_worklet.js` | Dedicated-thread mic capture AudioWorklet (ScriptProcessor fallback) feeding `/WSaudioTX` |
| TXModulator | Shared DSP | `../web_control/dsp.py` | Consume `/WSaudioTX` PCM, resample, Hilbert SSB modulate to 24-bit IQ, queue `0xFFFD` TX packets |
| BandPowerAPI | Backend core | `server.py` | `/api/band_power` GET/POST; persist per-band drive % to `band_power.json`; apply to device |
| ServiceWorker | Frontend support | `static/sw.js` | Static asset cache with JS/HTML bypass |
| RestartScript | Operations | `restart.sh` | Safe restart, port cleanup, log redirection |
| StartScript | Operations | `start.sh` | Simple foreground launcher with default port |

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

controls.js
  -> WSaudioRX receives Int16 frames
  -> decodeInt16Audio()
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
| Browser TX EQ | `static/modules/tx_audio_eq.js`, `static/controls.js` |
| Restart/ops | `restart.sh`, `start.sh`, `server.log` |

## 11.5 Component Gaps

| Missing/Incomplete Component | Expected Responsibility |
|------------------------------|-------------------------|
| ATRWebSocketHandler | Implement `/WSATR1000` for power/SWR and tuner control |
| CWPage/FT8Page/RecordingsPage | Provide targets currently linked by menu |
| AuthBackend | Replace cookie-only callsign prompt with real server-side identity if required |

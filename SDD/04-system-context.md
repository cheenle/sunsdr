# 4. System Context (APP 011)

## 4.1 Context Diagram

```text
HAM Operator
  |
  | HTTPS/WSS from mobile or desktop browser
  v
SunMRRC FastAPI Server
  | serves static UI
  | manages WebSocket clients
  | imports SunSDR protocol and DSP helpers
  |
  | UDP control 192.168.16.100:50001 -> 192.168.16.200:50001
  | UDP IQ bind 192.168.16.100:50002 <- SunSDR2 DX stream
  v
SunSDR2 DX
```

## 4.2 Actors

| Actor | Role |
|-------|------|
| HAM Operator | Uses browser UI to listen, tune, adjust DSP/gain/filter, key PTT, and monitor status |
| System Maintainer | Starts/restarts service, manages TLS certificates, checks logs, validates network routing |
| SunSDR2 DX | External radio device controlled through verified UDP binary protocol |
| Browser Runtime | Provides WebSocket, Web Audio, microphone APIs, touch input, service worker cache |

## 4.3 External Interfaces

| Interface | Protocol | Endpoint | Direction | Description |
|-----------|----------|----------|-----------|-------------|
| Static UI | HTTPS/HTTP | `/{p:path}` | Browser -> Server | Serves `index.html`, CSS, JS, manifest, images, wasm |
| Control WebSocket | WSS/WS | `/WSCTRX` | Browser <-> Server | Commands and state updates |
| RX Audio WebSocket | WSS/WS | `/WSaudioRX` | Server -> Browser | Int16 PCM audio frames |
| TX Audio WebSocket | WSS/WS | `/WSaudioTX` | Browser -> Server | Transport accepted; payload not yet applied to radio TX modulation |
| Spectrum WebSocket | WSS/WS | `/WSspectrum` | Server -> Browser | Quantized spectrum rows |
| SunSDR Control | UDP | device `:50001` | Server <-> Radio | Boot, frequency, PTT, gain, filter, tune commands |
| SunSDR IQ Stream | UDP | local bind `:50002` | Radio -> Server | 24-bit IQ packet stream, plus keepalive/control packets |
| TLS Certificate | File | `certs/fullchain.pem`, `certs/radio.vlsc.net.key` | Server local | HTTPS enablement |

## 4.4 Data Flows

| Flow | Description |
|------|-------------|
| RX signal flow | SunSDR IQ UDP -> decode 24-bit IQ -> DSP feed -> demodulated PCM -> resample to 16 kHz -> `/WSaudioRX` -> Web Audio playback |
| Spectrum flow | IQ -> FFT -> dB clip -> uint8 quantize (512 bytes, ~38 Hz) -> `/WSspectrum` -> client accumulates `WF_DECIMATE` frames (~3.8 Hz) -> per-row adaptive noise floor (`WF_PCTL`) + blue-sea bias (`WF_BIAS`) + gain (`WF_GAIN`) -> waterfall canvas |
| Control flow | UI action -> `/WSCTRX` command string -> `SunSDR2DXClient` or demodulator setter -> response/broadcast |
| Liveness/latency flow | Frontend sends no-colon `PING` -> `ws_ctrl()` answers `PONG` before command parsing -> frontend round-trip timer updates the status-bar latency (ms) |
| S-meter flow | FFT percentile -> `getSignalLevel:*` over `/WSCTRX` -> client asymmetric exponential smoothing (attack 0.5 / release 0.15) -> S-meter needle |
| PTT safety flow | UI PTT press/release -> `setPTT:*` -> radio PTT -> `getPTT:*` ack -> frontend retry/watchdog when release ack is missing |
| HTTPS flow | Uvicorn selects TLS cert/key at startup; frontend selects WSS when page protocol is HTTPS |

## 4.5 System Boundaries

| Boundary | Inside | Outside |
|----------|--------|---------|
| Browser boundary | UI state, audio playback, mic capture, PTT safety UX | Browser permission model and autoplay policy |
| Server boundary | WebSockets, static serving, IQ processing orchestration, TLS startup | Radio firmware, network routing, certificate issuance |
| Shared-code boundary | `../web_control/sunsdr_direct.py`, `dsp.py`, `wdsp_wrapper.py` | These are imported dependencies rather than local `sunmrrc` files |
| Device boundary | UDP packets sent/received by server | SunSDR2 DX hardware behavior and RF environment |

## 4.6 Contextual Constraints

- iOS Safari requires HTTPS for reliable `getUserMedia` and AudioContext behavior outside localhost.
- The current server binds SunSDR local UDP addresses directly as `192.168.16.100`, so deployment network topology is part of the design.
- The default radio host is `192.168.16.200`, configurable by `DEVICE_HOST` only for the control client; IQ bind/send paths still contain fixed addresses in `server.py`.
- `/WSATR1000` is not part of the current backend despite frontend references.

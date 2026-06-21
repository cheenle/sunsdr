# 10. Service Model (ART 0582)

## 10.1 Service Portfolio

| Service | Type | Status | Responsibility |
|---------|------|--------|----------------|
| StaticUIService | Core | Implemented | Serve mobile UI assets from `static/` |
| ControlService | Core | Implemented | WebSocket command parsing, radio/DSP dispatch, state responses |
| RXAudioService | Core | Implemented | Broadcast demodulated Int16 PCM to browser clients |
| TXAudioIngressService | Core | Transport only | Accept TX audio WebSocket connection; backend modulation not implemented |
| SpectrumService | Core | Implemented | Broadcast quantized FFT rows for waterfall |
| SunSDRDeviceService | Core | Implemented via shared import | Connect and command SunSDR2 DX over UDP |
| IQProcessingService | Core | Implemented | Receive IQ, decode samples, feed DSP, derive audio/spectrum |
| DSPService | Core | Implemented/conditional | Software demodulation and optional WDSP controls |
| TLSStartupService | Support | Implemented | Choose HTTPS or HTTP runtime |
| ProcessRestartService | Support | Implemented | Controlled restart and log capture through `restart.sh` |
| MemoryChannelService | Planned | Missing backend | Frontend expects `/api/mem_channels` |
| ATRService | Planned | Missing backend | Frontend references ATR status/control |
| CW/FT8/RecordingServices | Planned/legacy links | Missing pages/backend in this snapshot | Menu targets only |

## 10.2 Service Dependencies

```text
StaticUIService
  -> browser runtime

ControlService
  -> SunSDRDeviceService
  -> DSPService

RXAudioService
  -> IQProcessingService
  -> DSPService

SpectrumService
  -> IQProcessingService

IQProcessingService
  -> UDP socket bind/send
  -> StreamProcessor / SpectrumProcessor / AudioDemodulator

TLSStartupService
  -> certs/fullchain.pem
  -> certs/radio.vlsc.net.key
```

## 10.3 Service Interfaces

| Service | Input | Output | Protocol |
|---------|-------|--------|----------|
| StaticUIService | GET path | Static bytes or fallback `index.html` | HTTP/HTTPS |
| ControlService | Command strings | Response/broadcast strings | WS/WSS `/WSCTRX` |
| RXAudioService | PCM from DSP | Int16 binary frames | WS/WSS `/WSaudioRX` |
| TXAudioIngressService | Binary/text from browser | Currently none | WS/WSS `/WSaudioTX` |
| SpectrumService | Float spectrum array | uint8 binary frames | WS/WSS `/WSspectrum` |
| SunSDRDeviceService | Method calls | UDP packets and mirrored state | In-process + UDP |
| DSPService | IQ/audio samples and config setters | PCM, status dict | In-process |

## 10.4 Control Service Contract

| Command | Response | Notes |
|---------|----------|-------|
| `PING` | `PONG` | No colon; handled before command parsing; drives the round-trip latency readout |
| `getSignalLevel:<0-60>` | (server push) | Server-emitted per spectrum frame (~38 Hz); client applies asymmetric exponential smoothing before display |
| `getFreq:` | `getFreq:<hz>` | Defaults to 14074000 if unknown |
| `getMode:` | `getMode:<mode>` | DSP demodulator mode |
| `getPTT:` | `getPTT:true/false` | Radio mirrored PTT state |
| `setFreq:<hz>` | `getFreq:<hz>` | Calls `radio.set_frequency()` |
| `setMode:<mode>` | `getMode:<mode>` | Calls demodulator `set_mode()` |
| `setPTT:true/false` | `getPTT:true/false` | Calls radio and demodulator PTT setters |
| `tune:true/false` | none | Calls `radio.set_tune()` |
| `setAFGain:<0-100>` | none | Sets demodulator volume and radio volume |
| `setRFGain:<0-100>` | none | Calls radio RF gain |
| `setFilter:<lo>,<hi>` | none | Reconfigures demodulator filter and radio filter |
| `setWDSP*` | broadcast/state | Updates DSP settings and broadcasts where applicable |
| `s:` | `getPTT:false` | Safety backup to force RX |
| `cq:*` | `cq:complete` | Acknowledges frontend state; no server CQ playback |

## 10.5 Service Quality Targets

| Service | Quality Target |
|---------|----------------|
| StaticUIService | Serve current JS/HTML without stale service-worker interference |
| ControlService | Never silently ignore safety-critical PTT release when connection is alive |
| RXAudioService | Preserve continuous playback under normal LAN jitter |
| SpectrumService | Stay compact enough for mobile rendering (~512 bytes/frame, ~38 Hz); client accumulates ~10 frames per row and applies adaptive noise-floor contrast |
| TLSStartupService | Make production mobile path secure by default |

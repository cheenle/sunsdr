# 7. Subject Area Model (APP 408)

## 7.1 Subject Areas

```text
ClientSession
  owns WebSocket memberships and UI state

RadioControl
  owns SunSDR2 DX command state: frequency, PTT, gain, filter, tune

IQStream
  owns UDP packet intake and conversion to complex samples

RXAudioFlow
  owns demodulated PCM and browser playback delivery

SpectrumFlow
  owns FFT, S-meter estimation, server-side quantized waterfall frames,
  and browser-side waterfall rendering state (frame accumulation, adaptive
  noise floor) plus S-meter exponential smoothing

DSPConfiguration
  owns mode, WDSP switches, AGC, notches, filter settings

OperationalConfig
  owns host/port/TLS/runtime script behavior
```

## 7.2 Entity Definitions

| Entity | Attributes | Description |
|--------|------------|-------------|
| ClientSession | websocket, channel_type, connected_at | Runtime connection in one of `ctrl_clients`, `audio_rx_clients`, `audio_tx_clients`, `spectrum_clients` |
| RadioDevice | host, control_port, stream_port, connected | SunSDR2 DX hardware endpoint and connection state |
| RadioState | rx_freq, tx_freq, ptt, preamp, rf_gain, agc, filter_low, filter_high | State mirrored by `SunSDR2DXClient` and UI responses |
| IQPacket | sub_type, counter, payload, sample_count | UDP stream packet parsed by `_process_iq_stream()` |
| IQSample | i, q, normalized_value | 24-bit signed I/Q sample converted to complex64 |
| DemodulatorState | mode, ptt, volume, filter, agc_gain | Software demodulation state inside `AudioDemodulator` |
| RXAudioFrame | pcm_bytes, sample_rate, frame_length | Int16 PCM frame broadcast to `/WSaudioRX` clients |
| SpectrumFrame | bins, quantized_bytes, percentile | FFT-derived waterfall and S-meter source; server quantizes 512 dB bins to uint8 (0 = -120 dB, 255 = 0 dB) |
| WaterfallRenderState | accum_buffer, accum_count, decimate, noise_pctl, headroom, gain, bias | Browser-side waterfall rendering state in `controls.js`: accumulates ~10 frames (38 Hz -> ~3.8 Hz), computes a per-row adaptive noise floor (30th percentile), then stretches contrast with bias/gain over a blue->cyan->yellow->red colour ramp |
| SMeterState | smoothed_value, attack_alpha, release_alpha | Browser-side S-meter smoothing in `updateSMeter`: asymmetric exponential filter (attack alpha 0.5, release alpha 0.15) for a stable needle |
| WDSPConfig | available, enabled, nr2Level, nr2_enabled, nbEnabled, anfEnabled, nfEnabled, agcMode, notches | Runtime DSP feature configuration |
| TLSConfig | certfile, keyfile, disabled | HTTPS startup decision |
| FrontendPreference | callsign, AF gain, squelch, saved frequency cookies, WDSP cookie state | Browser-persisted settings |
| MemoryChannel | index, frequency, mode, label | Planned service-side memory record expected by frontend manager |

## 7.3 Relationships

| Relationship | Cardinality | Description |
|--------------|-------------|-------------|
| ClientSession -> RXAudioFrame | N:M | Multiple connected browsers may receive each broadcast audio frame |
| ClientSession -> SpectrumFrame | N:M | Spectrum clients receive the same quantized frame |
| RadioDevice -> RadioState | 1:1 | Client object mirrors latest known radio state |
| IQPacket -> IQSample | 1:N | Each valid stream packet contains many decoded samples |
| IQSample -> RXAudioFrame | N:1 | DSP accumulates many IQ samples into demodulated audio output |
| IQSample -> SpectrumFrame | N:1 | Spectrum processor accumulates IQ samples for FFT |
| DSPConfiguration -> RXAudioFrame | 1:N | Mode, filters, and WDSP settings affect subsequent audio frames |
| TLSConfig -> ClientSession | 1:N | HTTPS selection determines browser WSS and secure API availability |

## 7.4 State Ownership

| State | Owner | Persistence |
|-------|-------|-------------|
| Connected WebSockets | `server.py` global sets | Runtime only |
| Radio state | `SunSDR2DXClient` instance | Runtime only |
| DSP state | `AudioDemodulator` instance | Runtime only; frontend cookies may replay settings |
| UI state | `mobileState`, DOM, cookies | Browser session/cookies |
| Waterfall render state | `controls.js` waterfall module (`wfAccum`, `wfAccumCount`, `WF_*`) | Browser runtime only |
| Smoothed S-meter value | `mobileState.currentSMeter` via `updateSMeter` | Browser runtime only |
| TLS files | `certs/` | Filesystem |
| Server logs | `server.log` via `restart.sh` | Filesystem |

## 7.5 Deferred Domain Entities

| Entity | Reason Deferred |
|--------|-----------------|
| ATRMeterData | Frontend exists, backend endpoint absent |
| TXModulationFrame | Browser sends TX audio, but server-side RF/audio modulation path is not implemented |
| AuthUser | No server-side user database or auth routes in current backend |
| LogbookEntry | Menu item only; no model in current backend |

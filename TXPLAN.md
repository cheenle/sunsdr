# TX Current Status

> **Status: VERIFIED WORKING (2026-06-28)**

TX is fully implemented and verified. This document replaces the original TX implementation plan, which is now complete and obsolete.

## Verified performance

| Metric | Value | Notes |
|--------|-------|-------|
| SNR (far end) | 37 dB | Clean SSB, no audible artifacts |
| Dropouts | 0 | Jitter buffer absorbs browser GC pauses up to ~300 ms |
| SSB efficiency | 96% | 300 Hz HPF reclaims ~15% PA capacity vs no filter (was 69%) |
| Voice quality | Excellent | Clean, no clicking, no trembling, no envelope pumping |
| Power | ~80 W PEP @ 100% drive | Matches ExpertSDR3 reference |
| Tune mode | ~10 W continuous | Safe PA thermal level via TX_TUNE_SCALE=0.35 |

## Active TX path

All modulation is done in Python -- there is no WDSP TX C-chain.

```
Browser Mic → Opus encode → /WSaudioTX (WSS)
  → Opus decode → 16 kHz Int16 PCM
  → DC blocker (1st-order IIR HPF @ 20 Hz, continuous)
  → 300 Hz 4th-order Butterworth HPF
  → Anti-alias 4th-order Butterworth LPF @ 3.6 kHz
  → Fractional resampler (16000 Hz → 15625 Hz)
  → Overlap-save Hilbert SSB modulator (Python, sole path)
  → Flat drive gain (TX_DRIVE_GAIN × drive slider)
  → tanh soft limiter (ceiling = TX_IQ_PEAK = 1.0)
  → TX amplitude ramp (200-sample cosine fade)
  → 24-bit LE IQ packet encode
  → UDP 50002 (sub=0xFFFD, 195 pkts/s)
```

## SAB ring buffer

The client-side TX audio path uses a SharedArrayBuffer (SAB) ring buffer for low-latency audio transfer between the AudioWorklet (capture thread) and the Opus encoding worker. This avoids the latency and jitter of `postMessage()` for audio samples.

- **Module**: `sunmrrc/static/modules/tx_sab_ring.js`
- **Mechanism**: Atomic operations on shared memory, lock-free ring buffer
- **Benefit**: Eliminates `postMessage` serialization latency between AudioWorklet and Worker

## 300 Hz HPF

The 300 Hz 4th-order Butterworth highpass filter was added 2026-06-28. Analysis showed 38.7% of mic energy sits below 300 Hz (room rumble, proximity effect, subsonic noise), which SSB modulators suppress by default. Removing this band before modulation reclaims ~15% of PA headroom for the voice band, improving effective SSB efficiency from ~69% to ~96%. No clicks, no distortion -- the filter is continuous across frames via persistent `sosfilt` state.

## Level probes

Four-stage end-to-end level probes (in/an/drv/lim) capture RMS+peak at each gain stage. Server.py logs them at 1 Hz during TX via `snapshot_levels()`, providing full visibility into the gain chain. See `web_control/TX_CHAIN.md` for the complete API reference.

## Jitter buffer

Two-level hysteresis with TX_MIC_PRIME_PKTS=60 (first fill, ~307 ms) and TX_MIC_REPRIME_PKTS=20 (underflow recovery, ~102 ms). The adaptive pacer adjusts interval within +/- 25% based on queue depth, absorbing the ~5 pkt/s producer/consumer mismatch indefinitely.

## What was fixed along the way

- **B1**: TX IQ data was all zeros -- now calls `dsp_proc.get_tx_iq()`
- **B2**: Heartbeat trailer was 4 bytes -- now 8 bytes, preventing 8-minute session timeout
- **B3**: Heartbeat from separate socket -- now reuses `radio._sock`
- **B4**: PTT not released on shutdown -- lifespan cleanup calls `radio.set_ptt(False)` + `radio.disconnect()`
- **B5**: No RX stream keep-alive during TX -- 0xFFFE sent every 0.5 s during TX
- **WDSP TX C-chain**: Never activated, fully removed; Python Hilbert is the sole SSB path
- **Per-hop AGC**: Removed -- was amplitude-modulating the carrier, causing trembling voice
- **Hard magnitude clip**: Replaced with tanh soft limiter -- eliminates wideband splatter
- **Per-frame DC removal**: Replaced with continuous 1st-order IIR -- eliminates 50 clicks/sec

## Remaining gaps

- TX EQ is client-side only (browser AudioWorklet); no server-side TX EQ
- No ALC / compression in the TX chain (intentional -- flat gain preserves envelope)
- `/WSATR1000` is a placeholder (no real tuner hardware integration)

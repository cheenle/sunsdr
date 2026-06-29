# SunSDR2 DX -- TX Audio Chain

## Overview

The TX chain converts browser microphone audio into 24-bit LE IQ packets transmitted to the SunSDR2 DX hardware over UDP port 50002. All modulation is done in Python -- there is no WDSP TX C-chain.

```
Browser Mic → getUserMedia() → AudioWorklet → Opus encode → /WSaudioTX (WSS)
  → Opus decode → 16 kHz Int16 PCM
  → DC blocker (1st-order IIR HPF @ 20 Hz, continuous)
  → 300 Hz 4th-order Butterworth HPF
  → Anti-alias 4th-order Butterworth LPF @ 3.6 kHz
  → Fractional resampler (16000 Hz → 15625 Hz)
  → Overlap-save Hilbert SSB modulator (Python, sole path)
  → Flat drive gain (TX_DRIVE_GAIN × drive slider)
  → tanh soft limiter (ceiling = TX_IQ_PEAK = 1.0)
  → TX amplitude ramp (200-sample cosine fade)
  → 24-bit LE IQ packet encode (200 samples → 1200 bytes)
  → UDP 50002 (sub=0xFFFD, 195 pkts/s)
```

## Stage-by-stage detail

### 0. Input: Opus-decoded 16 kHz Int16 PCM

The browser captures mono audio via `getUserMedia()`, processes it through an AudioWorklet (client-side EQ + gain), Opus-encodes each frame (tag byte `0x01`), and sends binary frames over `/WSaudioTX`. The server decodes Opus back to 16 kHz Int16 PCM and feeds raw bytes to `TXModulator.feed_audio()`.

- **Sample rate**: 16000 Hz (fixed, regardless of browser hardware rate)
- **Format**: Signed 16-bit little-endian PCM
- **Frame size**: Variable (Opus frames), typically ~20 ms
- **Codec tag**: `0x01` = Opus, `0x00` = Int16 PCM fallback

### 1. DC blocker: 1st-order IIR highpass @ 20 Hz

**Continuous, no per-frame reset.** Previously `x -= x.mean()` was applied per-frame, which caused DC jumps at 76% of frame boundaries (up to 2.7% full-scale) -- producing ~50 audible clicks per second. The current implementation uses a persistent `scipy.signal.lfilter` with state that carries across `feed_audio()` calls.

- **Type**: 1st-order IIR highpass
- **Cutoff**: 20 Hz
- **Transfer function**: H(z) = (1 - z^-1) / (1 - R * z^-1), R = exp(-2*pi*20/16000)
- **Convergence**: ~250 ms at TX start (one mild transient), then continuous tracking
- **Reset**: State zeroed on PTT cycle via `reset_mic()`

### 2. 300 Hz 4th-order Butterworth HPF

Added 2026-06-28. Pre-modulation spectrum analysis showed 38.7% of mic energy sits below 300 Hz (room rumble, proximity effect, subsonic noise). SSB modulators suppress this band by default, so that energy is wasted -- it consumes headroom in the tanh limiter and drive gain chain without contributing to radiated power. This filter reclaims ~15% of PA capacity for the voice band, improving effective SSB efficiency from ~69% to ~96%.

- **Type**: 4th-order Butterworth IIR (second-order sections)
- **Cutoff**: 300 Hz
- **Design**: `scipy.signal.butter(4, 300, 'high', fs=16000, output='sos')`
- **Implementation**: `sosfilt` with persistent `zi` state (continuous across frames)
- **Reset**: State zeroed on PTT cycle via `reset_mic()`

### 3. Anti-alias LPF: 4th-order Butterworth @ 3.6 kHz

The 16 kHz input carries up to 8 kHz of energy, but after resampling to 15625 Hz the Nyquist frequency is only 7812.5 Hz. A gentle lowpass prevents content above ~7.8 kHz from folding back into the voice band.

- **Type**: 4th-order Butterworth IIR (second-order sections)
- **Cutoff**: 3600 Hz (gentle, complements browser-side EQ)
- **Design**: `scipy.signal.butter(4, 3600, 'low', fs=16000, output='sos')`
- **Implementation**: `sosfilt` with persistent `zi` state (continuous across frames)

### 4. Fractional resampler: 16000 Hz → 15625 Hz

A continuous linear-interpolation resampler with a persistent input buffer and fractional read cursor. No per-frame reset -- frame seams introduce no discontinuity.

- **Ratio**: 16000 / 15625 = 1.024 (slightly more input samples than output)
- **Method**: Linear interpolation with persistent phase accumulator (`_rs_phase`)
- **Output rate**: 15625 Hz (native audio rate, matches hardware)

### 5. Python Hilbert overlap-save SSB modulator (sole path)

**WDSP TX C-chain has been removed.** All SSB modulation is done in Python using `scipy.signal.hilbert` with overlap-save block processing.

- **Block size**: TX_AUDIO_PER_PKT = 80 audio samples (5.12 ms)
- **Overlap margin**: TX_HILBERT_MARGIN = 256 samples each side
- **Total block**: 2 * margin + block = 592 audio samples
- **Window advance**: 80 samples per hop (no overlap in output)
- **Upsampling**: Linear interpolation 80 → 200 IQ samples (×2.5, to 39063 Hz IQ rate)
- **Sideband**: USB = analytic signal, LSB = conjugate of analytic signal
- **Modes supported**: USB, LSB, AM, FM, CW

The overlap-save approach eliminates per-chunk edge transients from the Hilbert transform, producing clean, phase-continuous SSB output across frame boundaries.

### 6. Drive gain

A flat (non-AGC) linear make-up gain applied before the soft limiter.

- **TX_DRIVE_GAIN**: 2.8 (reduced from 3.5 on 2026-06-29)
- **Drive slider**: 0.0-1.0, scales gain linearly
- **Effective gain**: `TX_DRIVE_GAIN * self.drive`
- **No AGC**: Flat gain preserves the voice envelope. A per-hop tracking AGC used to live here but amplitude-modulated the carrier, causing a trembling/quivering voice at the far end -- it has been removed.

At 2.8: mic peak 0.75 * 2.8 = 2.1, tanh engages on ~25% of peaks (mild compression, no envelope riding). RMS output ~0.25 (vs ExpertSDR3 reference ~0.33). Turn device drive up to compensate -- drive scales RF power at the PA, not IQ amplitude.

### 7. tanh soft limiter

A soft magnitude limiter using `tanh()` -- NOT a hard clip. A hard magnitude clip creates sharp corners on every syllable, generating wideband splatter across the whole passband. `tanh` saturates smoothly toward the ceiling, rounding off transients with far less out-of-band energy.

- **Ceiling**: TX_IQ_PEAK = 1.0 (fixed, independent of drive)
- **Formula**: `limited = ceiling * tanh(magnitude / ceiling)`
- **Key property**: Drive moves the signal level under a FIXED ceiling, rather than moving the ceiling itself (the old bug that caused severe overdrive at every drive setting)

### 8. TX amplitude ramp

A linear gain ramp applied to the leading edge of TX IQ after the settling pad, removing the hard amplitude step from zero-IQ silence to full modulation. This eliminates one source of the TX start "click."

- **Length**: TX_RAMP_SAMPLES = 200 samples (~1 packet, ~5.1 ms at 39063 Hz)
- **Shape**: Linear 0→1 (cosine-like fade in practice due to tanh interaction)
- **Reset**: `reset_tx_ramp()` called on PTT assert

### 9. 24-bit LE IQ packet encoding

Each TX packet carries 200 complex IQ samples encoded as 24-bit signed little-endian interleaved I/Q pairs.

- **Packet size**: 200 samples * 6 bytes = 1200 bytes IQ payload
- **Header**: 10 bytes (magic 0xFF32, sub=0xFFFD, counter, flags=0x0102)
- **Total**: 1210 bytes per UDP packet
- **Packet rate**: ~195 pkts/s (TX_PACKET_INTERVAL_S = 5.12 ms)
- **IQ sample rate**: 39063 Hz (200 samples/pkt * 195 pkts/s)
- **Format per sample**: 6 bytes = I[0:3] + Q[3:6], 24-bit signed LE, range +/- 2^23 = +/- 8,388,608

### 10. TX settling pad

TX_SETTLE_PACKETS = 17 zero-IQ packets (~87 ms) sent immediately after PTT assert, giving the PA and relays time to settle before real modulation begins. The amplitude ramp then fades from this silence into full modulation.

## Jitter buffer

The mic path has two levels of buffering:

### Modulator queue (dsp.py TXModulator)
- **Queue**: `_mic_iq` deque, maxlen=1024 (~5.2 seconds at 195 pkts/s)
- **Prime strategy**: Two-level hysteresis
  - **First fill**: Must reach TX_MIC_PRIME_PKTS = 60 packets (~307 ms) before draining starts
  - **Re-prime after underflow**: TX_MIC_REPRIME_PKTS = 20 packets (~102 ms)
- **Underflow behavior**: Returns None → pacer sends silence; counter `_mic_underruns` increments
- **Thread safety**: `_mic_lock` (WS thread writes via `feed_audio()`, pacer thread reads via `get_mic_iq()`)

### Adaptive pacer (server.py `_tx_pacer_thread`)
- **Pacing**: `time.sleep()`-based, target TX_PACKET_INTERVAL_S = 5.12 ms
- **Adaptive window**: +/- 25% interval adjustment based on EMA-smoothed queue depth
- **Target queue depth**: 80 packets (~410 ms)
- **EMA alpha**: 0.15 (~7 packets, ~35 ms response)
- **No server-side de-prime**: The pacer never resets its buffer. If the WS stream stalls, the modulator's own two-level hysteresis absorbs the gap; the pacer sends silence during the re-prime period and resumes cleanly.

## Level probes

The modulator maintains four RMS+peak accumulators that capture per-hop magnitude at each gain stage:

| Stage | Key | Description |
|-------|-----|-------------|
| `in` | `_lvl_in_*` | Input audio magnitude (pre-drive, pre-Hilbert), divided back by `gain` |
| `an` | `_lvl_an_*` | Analytic signal magnitude (post-Hilbert), divided back by `gain` |
| `drv` | `_lvl_drv_*` | Post-drive magnitude (after `TX_DRIVE_GAIN * drive`, before limiter) |
| `lim` | `_lvl_lim_*` | Post-limiter magnitude (after `tanh`, final IQ amplitude) |

### snapshot_levels() API

```python
def snapshot_levels(self) -> dict:
    """Return + reset the per-stage level accumulators.

    Called by server.py at 1 Hz to log end-to-end gain across the TX chain.

    Returns dict with keys:
        in_sq, in_pk, in_n    -- input stage (sum of squares, peak, count)
        an_sq, an_pk, an_n    -- analytic stage
        drv_sq, drv_pk, drv_n  -- post-drive stage
        lim_sq, lim_pk, lim_n  -- post-limiter stage

    RMS for each stage = sqrt(sq / n), peak = pk.
    """
```

Server.py calls `snapshot_levels()` at 1 Hz during TX and logs computed RMS/peak values, providing end-to-end visibility into the gain chain.

## PTT lifecycle

1. Client sends `set_ptt:true` over `/WSCTRX`
2. Server calls `radio.set_ptt(True)` -- sends DRIVE (0x0017) + PTT ON
3. Server resets modulator: `reset_tx_ramp()` + `reset_mic()` (clears stale audio, re-arms DC/HPF filter state, resets jitter buffer prime, resets level probes)
4. Dedicated `_tx_pacer_thread` starts:
   - Sends TX_SETTLE_PACKETS (17) zero-IQ packets (PA/relay settling)
   - Applies amplitude ramp over first TX_RAMP_SAMPLES (200) of real IQ
   - Drains modulator's jitter-buffered IQ queue, with adaptive pacing
   - Tracks TX IQ power stats (RMS, peak) at 1 Hz
   - Sends 0xFFFE keep-alive packets every 0.5 s during TX
5. During TX: main loop receives telemetry (0x1F00: watts, volts, temperature) non-blocking
6. Client sends `set_ptt:false` (or `s:` emergency release, or watchdog timeout)
7. Server calls `radio.set_ptt(False)` -- PTT OFF + stream restore (0x0006 + 0x0020)
8. `tx_thread_stop = True`; pacer thread joins within 1 s
9. Flush DC-blocked diagnostic capture to `sunmrrc/captures/tx_post_dcblock_*.wav`
10. Resume normal RX IQ processing

## Files

| File | Role |
|------|------|
| `web_control/dsp.py` | TXModulator class: filters, resampler, Hilbert SSB, encode, jitter buffer, level probes |
| `sunmrrc/server.py` | `/WSaudioTX` handler, PTT lifecycle, TX pacer thread, packet construction, telemetry |
| `sunmrrc/static/controls.js` | Client TX audio capture, AudioWorklet, Opus encode, PTT button state machine |
| `sunmrrc/static/modules/tx_sab_ring.js` | SAB ring buffer for TX audio path (low-latency shared memory) |
| `sunmrrc/static/tx_opus_worker.js` | Opus encoding worker for TX audio |
| `sunmrrc/static/tx_capture_worklet.js` | AudioWorklet for mic capture |
| `web_control/sunsdr_direct.py` | `set_ptt()`, `set_tune()`, DRIVE command (0x0017), hardware control |

## Key constants

| Constant | Value | Description |
|----------|-------|-------------|
| `TX_IQ_PEAK` | 1.0 | Full-scale IQ ceiling (tanh knee) |
| `TX_DRIVE_GAIN` | 2.8 | Flat make-up gain before limiter |
| `TX_TUNE_SCALE` | 0.35 | Tune carrier amplitude fraction (~10 W safe level) |
| `TX_SETTLE_PACKETS` | 17 | Zero-IQ packets for PA/relay settling (~87 ms) |
| `TX_RAMP_SAMPLES` | 200 | Amplitude ramp length (~5.1 ms) |
| `TX_MIC_PRIME_PKTS` | 60 | Jitter buffer first-fill watermark (~307 ms) |
| `TX_MIC_REPRIME_PKTS` | 20 | Jitter buffer re-prime watermark (~102 ms) |
| `TX_HILBERT_MARGIN` | 256 | Overlap-save context samples each side |
| `TX_AUDIO_PER_PKT` | 80 | Audio samples per modulation hop (5.12 ms) |
| `TX_PACKET_SAMPLES` | 200 | IQ samples per packet |
| `TX_PACKET_INTERVAL_S` | 0.00512 | Pacing interval between packets |

## No WDSP TX C-chain

WDSP (`libwdsp.dylib`) is used only for RX demodulation (AGC, NR2, NB, ANF). All TX modulation -- filtering, resampling, Hilbert SSB, limiting -- is implemented in pure Python/NumPy/SciPy. The earlier WDSP TX C-chain was never activated and has been fully removed from the codebase.

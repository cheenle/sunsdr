"""
SunSDR2 DX IQ Stream Processor
===============================
IQ → IF shift → complex SSB bandpass → real extraction → decimation → AGC → audio.

True IQ sample rate: 78125 Hz (5^7)
RX DDS = VFO + 30500 Hz IF offset
Audio output: 15625 Hz (5× decimation)
"""

import math, logging, struct, threading
import numpy as np
from collections import deque
from dataclasses import dataclass
from scipy.signal import hilbert

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────

IQ_SAMPLE_RATE = 78125      # RX IQ rate, verified: 390.7 pkt/s × 200 samples
IF_OFFSET = 30500.0         # RX DDS - TX VFO (verified from pcap)
AUDIO_DECIM = 5
AUDIO_RATE = IQ_SAMPLE_RATE // AUDIO_DECIM   # 15625 Hz
FFT_SIZE = 2048


def set_iq_sample_rate(hz: int) -> int:
    """Update the global IQ sample rate, audio rate, and decimation factor."""
    global IQ_SAMPLE_RATE, AUDIO_RATE, AUDIO_DECIM
    IQ_SAMPLE_RATE = hz
    AUDIO_DECIM = max(1, round(hz / 15625))
    AUDIO_RATE = hz // AUDIO_DECIM
    logger.info("IQ sample rate: %d Hz → decim=%d → audio: %d Hz",
                 hz, AUDIO_DECIM, AUDIO_RATE)
    return AUDIO_RATE

# ── TX chain (verified from device/captures/sunsdr_sdr_tx.pcap) ─────
# ExpertSDR3 transmits IQ at HALF the RX rate: 5.12 ms/packet × 200
# samples = 39,063 Hz (5^7 / 2), the lowest of the manual's 39/78/156/312
# kHz options. See PROTOCOL.md §17 and device/data/tx_analysis.json.
TX_IQ_SAMPLE_RATE = 39063           # measured 195.8 pkt/s × 200 samples
TX_PACKET_SAMPLES = 200             # IQ samples per 0xFFFD packet
TX_PACKET_INTERVAL_S = TX_PACKET_SAMPLES / TX_IQ_SAMPLE_RATE   # 5.12 ms
# Audio samples consumed per TX packet to stay time-coherent:
#   200 IQ @ 39063 Hz (5.12 ms) == 80 audio @ 15625 Hz
TX_AUDIO_PER_PKT = round(TX_PACKET_SAMPLES * AUDIO_RATE / TX_IQ_SAMPLE_RATE)  # 80
# TX IQ ceiling = FULL SCALE (1.0). VERIFIED against a fresh ExpertSDR3 capture
# (device/captures/expert_40m_drive.pcap, 2026-06-24): genuine voice TX IQ runs
# at RMS≈0.33 with peaks routinely hitting 1.0 (full 24-bit scale), and Tune is a
# near-full-scale constant carrier (RMS≈0.98, peak 1.0). The IQ amplitude is
# INDEPENDENT of drive — across drive bytes 114→255 (20%→100%) voice RMS held
# steady at ~0.33. Drive (0x0017) scales RF power at the DEVICE; the digital IQ
# is always sent hot.
#
# The earlier "~0.092 peak" figure was wrong: it was measured off a quiet/low
# segment of an old capture, not a full-modulation burst. Capping at 0.5 was the
# root cause of the low-power bug — it threw away half the amplitude (RMS 0.20 vs
# ExpertSDR3's 0.33), so 100% drive made only ~20 W where ExpertSDR3 makes ~45 W
# (the measured 2.25× power gap matches (0.33/0.20)² exactly). At 1.0 the existing
# TX_DRIVE_GAIN drives peaks into the tanh knee near 1.0 and lifts RMS to ~0.33,
# matching the genuine client. Device drive remains the RF power control.
TX_IQ_PEAK = 1.0
# Tune carrier amplitude scale (fraction of TX_IQ_PEAK). Tune is a CONTINUOUS
# constant-envelope tone (RMS ≈ peak), so its average power equals its PEP —
# far more thermal load on the PA than voice (which is ~3:1 crest factor, low
# average). At full scale Tune hits ~80 W continuous, which stresses the PA.
# Scale the tune wav down so Tune lands at a safe ~10 W for antenna tuning,
# WITHOUT touching voice power (voice uses TX_DRIVE_GAIN, a separate path).
# Power vs scale is ∝ amplitude² (verified empirically: scale 0.15 → 1.7 W,
# matching 80 W × 0.15² = 1.8 W). So target ~10 W → scale = √(10/80) ≈ 0.35.
TX_TUNE_SCALE = 0.35
# Leading zero-IQ packets after PTT assert, covering PA/relay settling
# (~17 zero packets observed before audio energy in the reference burst).
TX_SETTLE_PACKETS = 17
# Linear amplitude ramp applied at TX start to avoid a hard step from the
# zero-IQ settling pad to full-amplitude modulation (one source of the click).
# ~1 packet (200 samples @ 39063 Hz ≈ 5.1 ms).
TX_RAMP_SAMPLES = 200
# Mic jitter buffer: WS delivers ~20 ms voice frames in bursts (and the
# browser ScriptProcessor fires every ~43 ms), but the TX pacer drains one
# 5.12 ms packet at a time. Without buffering, the queue oscillates near empty
# and underflows on any WiFi/GC/scheduling jitter — each underflow inserts
# silence and steps the amplitude to 0 (periodic clicking / stutter).
#
# Two-level hysteresis:
#   - First fill must reach TX_MIC_PRIME_PKTS (~120 ms) before draining starts.
#     A 120 ms cushion absorbs a single 40–60 ms late burst without hitting 0.
#   - After an underflow we only re-fill to TX_MIC_REPRIME_PKTS (~60 ms), so a
#     momentary stall costs a 60 ms re-buffer, not a full 120 ms gap. The old
#     code force-refilled the FULL prime depth on every underflow, which
#     amplified a 20 ms network hiccup into a 60 ms silence hole — the dropout
#     the far end heard as a "stutter".
TX_MIC_PRIME_PKTS = 24       # ~120 ms high watermark: initial fill before drain
TX_MIC_REPRIME_PKTS = 12     # ~60 ms: cheaper re-fill after an underflow
# Audio-rate samples carried across feed_audio() calls so each streaming
# Hilbert transform has history/look-ahead context and no per-chunk edge
# transient (buzz). Trimmed off both ends after the transform.
TX_HILBERT_MARGIN = 256
# Mic TX make-up gain: phone mic audio (post browser EQ/compression) lands
# around 0.1–0.3 peak, so a flat ×TX_IQ_PEAK gain alone transmits at a
# fraction of full power. We apply a FIXED linear make-up gain scaled by the
# drive control (0..1), then a hard safety clip at the TX_IQ_PEAK ceiling.
#
# NOTE: a per-hop tracking AGC used to live here. It re-scaled amplitude every
# 5.12 ms hop (~195×/sec), which amplitude-modulated the SSB carrier and made
# the far end hear a trembling/quivering voice. It has been removed — a flat
# gain is constant-envelope-preserving, so the audio sounds clean. The drive
# slider is now the only level control.
# NOTE 2: the safety limit is now a SOFT tanh knee, NOT a hard magnitude clip.
# A hard clip on IQ magnitude is a non-linear corner that splatters energy
# across the whole passband (the broadband "noise" the far end heard). tanh
# compresses transients smoothly with far less out-of-band splatter. The
# ceiling is fixed at TX_IQ_PEAK and does NOT scale with drive, so drive moves
# the level under a constant clip point instead of moving the clip point too
# (the old bug that guaranteed ~7× overdrive at every drive setting).
TX_DRIVE_GAIN = 3.0           # fixed make-up gain. RF power is set at the
# DEVICE (0x0017 drive); this only lifts the mic audio into a healthy digital
# level. LOWERED 5.0→3.0 (2026-06-24): the browser now does the peak-average
# reduction (tx_audio_eq.js compressor ratio 6:1 + makeup gain), so the IQ
# arriving here is already low-crest-factor. At ×5 the post-drive peak hit ~2.3
# (4.6× over the 0.5 ceiling) and the tanh knee did ALL the limiting — heavy
# compression distortion on loud syllables. At ×3 the tanh is a safety net for
# residual transients, not the primary limiter, so the far end hears cleaner
# SSB while the higher RMS (from client compression) lifts average power.
# Turn the DEVICE drive up for more peak power, not this.

# ── IQ Decoder ─────────────────────────────────────────────────────

def decode_iq_24bit(data: bytes) -> np.ndarray:
    """Decode 200 samples of 24-bit signed LE IQ from a UDP packet."""
    n = min(200, len(data) // 6)
    iq = np.zeros(n, dtype=np.complex64)
    for i in range(n):
        off = i * 6
        if off + 6 > len(data): break
        i_val = int.from_bytes(data[off:off+3], 'little', signed=True)
        q_val = int.from_bytes(data[off+3:off+6], 'little', signed=True)
        iq[i] = complex(i_val / 8388608.0, q_val / 8388608.0)
    return iq


# ── Spectrum Processor ─────────────────────────────────────────────

class SpectrumProcessor:
    """Accumulates IQ and computes FFT for waterfall."""

    def __init__(self, fft_size: int = FFT_SIZE):
        self.fft_size = fft_size
        self.buf = np.zeros(fft_size, dtype=np.complex64)
        self.idx = 0
        self.win = np.hanning(fft_size).astype(np.float32)

    def feed(self, iq: np.ndarray) -> np.ndarray | None:
        n = len(iq)
        space = self.fft_size - self.idx
        if n >= space:
            self.buf[self.idx:] = iq[:space]
            windowed = self.buf * self.win
            spec = np.fft.fftshift(np.abs(np.fft.fft(windowed)))
            spec_db = np.clip(20 * np.log10(spec + 1e-10), -120, 0)
            remaining = n - space
            if remaining > 0:
                self.buf[:remaining] = iq[space:space+remaining]
                self.idx = remaining
            else:
                self.idx = 0
            return spec_db.astype(np.float32)
        else:
            self.buf[self.idx:self.idx+n] = iq
            self.idx += n
            return None

    @staticmethod
    def bin(spec: np.ndarray, n_bins: int = 512) -> list:
        if len(spec) < n_bins: return spec.tolist()
        f = len(spec) // n_bins
        return np.mean(spec[:f*n_bins].reshape(n_bins, f), axis=1).tolist()


# ── Audio Demodulator ──────────────────────────────────────────────

class AudioDemodulator:
    """IQ → audio: IF shift, complex SSB bandpass, real extraction, decimate, AGC."""

    def __init__(self, sample_rate: int = IQ_SAMPLE_RATE,
                 audio_rate: int = AUDIO_RATE):
        self.sample_rate = sample_rate
        self.audio_rate = audio_rate
        self.decim = AUDIO_DECIM
        self.mode = "USB"
        self.audio_buffer = deque(maxlen=audio_rate * 2)
        self._lo_phase = 0.0
        self._volume = 0.5
        self._agc_gain: float | None = None

        self._build_filters()
        self._init_wdsp()

    # ── Filter design ──────────────────────────────────────────

    def _build_filters(self):
        """Complex SSB bandpass filters (isolate one sideband)."""
        from gr4_filters import design_fir_lowpass
        proto = design_fir_lowpass(
            cutoff=1500.0, sample_rate=float(self.sample_rate),
            attenuation_db=60.0, transition_width_hz=600.0,
            window_type="hamming").astype(np.float64)
        idx = np.arange(len(proto), dtype=np.float64)
        wc = 2.0 * math.pi * 1500.0 / float(self.sample_rate)
        self._bp_usb = (proto * np.exp(1j * wc * idx)).astype(np.complex128)
        self._bp_lsb = (proto * np.exp(-1j * wc * idx)).astype(np.complex128)
        self._lpf = design_fir_lowpass(
            cutoff=3200.0, sample_rate=float(self.sample_rate),
            attenuation_db=60.0, transition_width_hz=800.0,
            window_type="hamming").astype(np.float64)
        self._st_usb = np.zeros(len(self._bp_usb) - 1, dtype=np.complex128)
        self._st_lsb = np.zeros(len(self._bp_lsb) - 1, dtype=np.complex128)
        self._st_lpf = np.zeros(len(self._lpf) - 1, dtype=np.float64)

    # ── WDSP (optional) ────────────────────────────────────────

    def _init_wdsp(self):
        self._wdsp = None
        self._wdsp_warm = 0
        self._wdsp_accum = np.array([], dtype=np.float32)
        self._wdsp_bs = 256  # must match buffer_size (no zero-padding!)
        # ── WDSP state tracking ────────────────────────────────
        self._wdsp_enabled = True   # enabled by default
        self._nr2_enabled = True    # NR2 on by default
        self._nr2_level = 30
        self._nr2_gain_method = 0
        self._nr2_npe_method = 0
        self._nr2_ae_run = True
        self._nb_enabled = False
        self._anf_enabled = False
        self._nf_enabled = False
        self._agc_mode = 3  # SLOW
        self._bp_low = 200.0
        self._bp_high = 3000.0
        self._notches: list[dict] = []
        try:
            import ctypes, os
            _lib = None
            for p in ["/usr/local/lib/libwdsp.dylib", "/opt/homebrew/lib/libwdsp.dylib"]:
                if os.path.exists(p): _lib = p; break
            if not _lib: return
            import wdsp_wrapper
            wdsp_wrapper._wdsp = ctypes.CDLL(_lib)
            wdsp_wrapper.WDSP_AVAILABLE = True
            from wdsp_wrapper import WDSPProcessor, WDSPMode, WDSPAGCMode
            self._wdsp = WDSPProcessor(
                sample_rate=int(self.audio_rate), buffer_size=self._wdsp_bs,
                mode=WDSPMode.USB, enable_nr2=True, enable_nb=False,
                enable_anf=False, agc_mode=WDSPAGCMode.SLOW)
            # Apply the default NR2 level (50) — was silently ignored before.
            self._wdsp.set_nr2_level(self._nr2_level)
            logger.info("WDSP ready (AGC SLOW + NR2 level=%d)", self._nr2_level)
        except Exception as e:
            logger.debug(f"WDSP unavailable: {e}")

    # ── Control ────────────────────────────────────────────────

    def set_sample_rate(self, hz: int):
        """Update IQ sample rate and rebuild all rate-dependent components."""
        if hz == self.sample_rate:
            return
        self.sample_rate = hz
        self.decim = max(1, round(hz / 15625))
        self.audio_rate = hz // self.decim
        self.audio_buffer = deque(maxlen=self.audio_rate * 2)
        self._build_filters()
        self._st_usb.fill(0); self._st_lsb.fill(0); self._st_lpf.fill(0)
        if self._wdsp:
            try:
                self._wdsp.close()
            except: pass
            self._wdsp = None
        self._init_wdsp()
        logger.info("AudioDemodulator rate: %d Hz, decim=%d, audio: %d Hz",
                     self.sample_rate, self.decim, self.audio_rate)

    def set_mode(self, mode: str):
        self.mode = mode.upper()
        if self._wdsp:
            try:
                from wdsp_wrapper import WDSPMode
                m = {"USB": WDSPMode.USB, "LSB": WDSPMode.LSB,
                     "CW": WDSPMode.CW, "AM": WDSPMode.AM}.get(self.mode, WDSPMode.USB)
                self._wdsp.set_mode(m)
            except: pass

    def set_volume(self, vol: float):
        self._volume = max(0.0, min(1.0, vol))

    def set_ptt(self, tx: bool):
        if tx:
            self.audio_buffer.clear()
            self._agc_gain = None

    def reconfigure_filter(self, low_hz: int = 200, high_hz: int = 3000):
        cutoff = max(min(high_hz, 4000), 500)
        from gr4_filters import design_fir_lowpass
        proto = design_fir_lowpass(
            cutoff=float(cutoff), sample_rate=float(self.sample_rate),
            attenuation_db=60.0, transition_width_hz=min(600, cutoff//2),
            window_type="hamming").astype(np.float64)
        idx = np.arange(len(proto), dtype=np.float64)
        wc = 2.0 * math.pi * (cutoff / 2.0) / float(self.sample_rate)
        self._bp_usb = (proto * np.exp(1j * wc * idx)).astype(np.complex128)
        self._bp_lsb = (proto * np.exp(-1j * wc * idx)).astype(np.complex128)
        self._st_usb.fill(0); self._st_lsb.fill(0)

    # ── WDSP control methods ────────────────────────────────────

    def get_wdsp_status(self) -> dict:
        """Return full WDSP status for front-end sync."""
        notches = [{"id": n["id"], "freq": n["freq"], "width": n["width"]}
                   for n in self._notches]
        return {
            "enabled": self._wdsp_enabled,
            "nr2": self._nr2_enabled,
            "nr2Level": self._nr2_level,
            "nb": self._nb_enabled,
            "anf": self._anf_enabled,
            "nf": self._nf_enabled,
            "agcMode": self._agc_mode,
            "notches": notches,
            "available": self._wdsp is not None,
        }

    def set_wdsp_enabled(self, on: bool):
        self._wdsp_enabled = on

    def set_nr2_level(self, level: int):
        self._nr2_level = max(0, min(100, level))
        if self._wdsp:
            self._wdsp.set_nr2_level(self._nr2_level)

    def set_nr2_enabled(self, on: bool):
        self._nr2_enabled = on
        if self._wdsp:
            self._wdsp.set_nr2_enabled(on)

    def set_nb_enabled(self, on: bool):
        self._nb_enabled = on
        if self._wdsp:
            self._wdsp.set_nb_enabled(on)

    def set_anf_enabled(self, on: bool):
        self._anf_enabled = on
        if self._wdsp:
            self._wdsp.set_anf_enabled(on)

    def set_nf_enabled(self, on: bool):
        self._nf_enabled = on
        if self._wdsp:
            self._wdsp.set_nf_enabled(on)

    def set_agc_mode(self, mode: int):
        """Set AGC mode: 0=OFF, 1=LONG, 2=SLOW, 3=MED, 4=FAST."""
        self._agc_mode = max(0, min(4, mode))
        if self._wdsp:
            self._wdsp.set_agc(self._agc_mode)

    def add_notch(self, freq_hz: float, width_hz: float):
        idx = len(self._notches)
        self._notches.append({"id": idx, "freq": freq_hz, "width": width_hz})
        if self._wdsp:
            self._wdsp.set_nf_enabled(True, freq_hz)
        return idx

    def edit_notch(self, idx: int, freq_hz: float, width_hz: float):
        for n in self._notches:
            if n["id"] == idx:
                n["freq"] = freq_hz
                n["width"] = width_hz
                break
        if self._wdsp:
            self._wdsp.set_nf_enabled(True, freq_hz)

    def set_nr2_gain_method(self, method: int):
        """Set NR2 gain method (0=linear, 1=log)."""
        self._nr2_gain_method = max(0, min(1, method))
        if self._wdsp:
            self._wdsp.set_nr2_gain_method(self._nr2_gain_method)

    def set_nr2_npe_method(self, method: int):
        """Set NR2 NPE method (0=standard)."""
        self._nr2_npe_method = max(0, min(1, method))
        if self._wdsp:
            self._wdsp.set_nr2_npe_method(self._nr2_npe_method)

    def set_nr2_ae_run(self, on: bool):
        """Enable/disable NR2 AE (acquisition enhancement) run."""
        self._nr2_ae_run = on
        if self._wdsp:
            self._wdsp.set_nr2_ae_run(on)

    def set_bandpass(self, low_hz: float, high_hz: float):
        """Set WDSP audio bandpass filter edges."""
        self._bp_low = low_hz
        self._bp_high = high_hz
        if self._wdsp:
            self._wdsp.set_bandpass(low_hz, high_hz)

    def delete_notch(self, idx: int):
        self._notches = [n for n in self._notches if n["id"] != idx]
        if self._wdsp and not self._notches:
            self._wdsp.set_nf_enabled(False)

    # ── Demodulation ───────────────────────────────────────────

    def demodulate(self, iq: np.ndarray) -> np.ndarray | None:
        """IQ → audio with IF shift, sideband selection, decimation, AGC."""
        if len(iq) == 0:
            return None

        # IF shift: VFO sits at -IF_OFFSET, shift up so VFO→0 Hz
        n = len(iq)
        dphi = 2.0 * math.pi * IF_OFFSET / float(self.sample_rate)
        phases = self._lo_phase + dphi * np.arange(n, dtype=np.float64)
        self._lo_phase = float((phases[-1] + dphi) % (2.0 * math.pi))
        bb = iq * np.exp(1j * phases)

        # Sideband selection + detection
        from scipy.signal import lfilter
        mode = self.mode
        if mode == "AM":
            audio = np.abs(bb).astype(np.float64)
            audio, self._st_lpf = lfilter(self._lpf, [1.0], audio, zi=self._st_lpf)
            audio -= np.mean(audio)
        elif mode in ("NFM", "FM", "WFM"):
            ang = np.angle(bb)
            audio = np.diff(np.unwrap(ang), prepend=ang[0]).astype(np.float64)
            audio, self._st_lpf = lfilter(self._lpf, [1.0], audio, zi=self._st_lpf)
        elif mode == "LSB":
            filt, self._st_lsb = lfilter(self._bp_lsb, [1.0], bb, zi=self._st_lsb)
            audio = np.real(filt).astype(np.float64)
        else:  # USB, CW, DIGI
            filt, self._st_usb = lfilter(self._bp_usb, [1.0], bb, zi=self._st_usb)
            audio = np.real(filt).astype(np.float64)

        # Decimate
        audio = audio[::self.decim]

        # Built-in AGC — only used when WDSP is unavailable or disabled.
        # When WDSP is active its own AGC (SLOW/MED/FAST) does the work;
        # we run a very slow low-gain pass just to keep the input healthy.
        if len(audio) > 0:
            rms = float(np.sqrt(np.mean(audio**2) + 1e-12))
            if self._agc_gain is None:
                self._agc_gain = 0.25 / rms if rms > 1e-10 else 1.0
            if self._wdsp_enabled and self._wdsp is not None:
                # WDSP active: its own AGC handles output level.
                # Gentle pre-gain just to keep input healthy for NR2.
                target = 0.2
                self._agc_gain = self._agc_gain * 0.995 + (target / (rms + 1e-10)) * 0.005
                audio *= min(self._agc_gain, 30.0)
            else:
                # Original AGC — unchanged from HEAD.
                target = 0.25 * self._volume * 2.0
                self._agc_gain = self._agc_gain * 0.95 + (target / (rms + 1e-10)) * 0.05
                audio *= min(self._agc_gain, 50000.0)
                np.clip(audio, -1.0, 1.0, out=audio)

        audio = audio.astype(np.float32)

        # WDSP post-processing (NR2, NB, ANF, AGC)
        if self._wdsp_enabled and self._wdsp is not None and len(audio) > 0:
            try:
                audio = self._wdsp.process(audio)
            except Exception:
                pass

        return audio

    # ── Audio chunk ────────────────────────────────────────────

    def get_audio_chunk(self) -> bytes | None:
        if len(self.audio_buffer) < 512: return None
        chunk = np.array([self.audio_buffer.popleft() for _ in range(512)])
        return (np.clip(chunk * 32767, -32768, 32767).astype(np.int16)).tobytes()


# ── TX IQ Modulator ────────────────────────────────────────────────

def _pack_iq_24bit(iq: np.ndarray) -> bytes:
    """Vectorized 24-bit signed LE I/Q packing.

    Replaces the per-sample struct.pack loop (200 samples × 2 = 400 pure-Python
    pack calls per packet, ~3000/burst) that held the GIL on the asyncio thread
    long enough to starve the TX pacer thread and jitter the 5.12 ms packet
    cadence (heard as a trembling/quivering voice at the far end). Now the whole
    packet is built with a handful of numpy ops that release the GIL.

    Layout per sample (6 bytes): I[0:3] then Q[0:3], each 24-bit signed LE.
    """
    n = len(iq)
    if n == 0:
        return b''
    # Interleave I,Q into one real array: [I0,Q0,I1,Q1,...]
    inter = np.empty(2 * n, dtype=np.float64)
    inter[0::2] = np.real(iq)
    inter[1::2] = np.imag(iq)
    # Scale to 24-bit full scale, clip to signed 24-bit range, then truncate
    # toward zero — matching the original int(np.clip(...)) per-sample behavior
    # exactly so the TX signal is byte-identical to the verified reference.
    vals = np.trunc(np.clip(inter * 8388608.0, -8388608, 8388607)).astype(np.int32)
    # Take the low 3 bytes of each int32 (little-endian): view as bytes, drop
    # the high byte of each 4-byte group. int32 LE byte order is b0 b1 b2 b3,
    # and for values in [-2^23, 2^23) the low 3 bytes are the correct 24-bit LE
    # two's-complement representation.
    b4 = vals.view(np.uint8).reshape(-1, 4)
    return b4[:, :3].tobytes()


class TXModulator:
    """Audio → SSB IQ: upsampling + Hilbert analytic signal → 24-bit IQ packets.

    Output matches the TX stream format (sub=0xFFFD, 200 samples × 6 bytes).
    Supports USB, LSB, AM, FM, CW modes.
    """

    def __init__(self, audio_rate: int = AUDIO_RATE, iq_rate: int = TX_IQ_SAMPLE_RATE):
        self.audio_rate = audio_rate       # 15625 Hz
        self.iq_rate = iq_rate             # 39063 Hz (TX rate = RX/2, verified)
        # Audio→IQ upsample ratio (39063/15625 = 2.5). 80 audio → 200 IQ.
        self.up_ratio = iq_rate / audio_rate
        self.mode = "USB"
        self.drive = 1.0                   # 0..1, scales below TX_IQ_PEAK ceiling
        self._audio_buf = np.array([], dtype=np.float32)
        self._phase = 0.0
        # Tune mode state
        self._tune_active = False
        self._tune_wav = np.array([], dtype=np.float32)  # pre-loaded WAV at audio_rate
        self._tune_iq = np.array([], dtype=np.complex64)  # pre-computed IQ-rate analytic signal
        self._tune_pos = 0
        # TX amplitude ramp: counts samples emitted since TX (re)start so the
        # first TX_RAMP_SAMPLES are scaled 0→1, removing the settling-pad step.
        self._ramp_pos = TX_RAMP_SAMPLES  # start fully ramped (no ramp until reset)
        # Live mic IQ queue: WS thread pushes encoded packets via feed_audio();
        # the TX pacer thread (different OS thread) pops via get_mic_iq().
        self._mic_lock = threading.Lock()
        self._mic_iq = deque(maxlen=1024)  # ~5 s of 5.12 ms packets; deep enough
        # to absorb the ~5 pkt/s production/consumption mismatch (browser ≈43
        # pkt/s, pacer ≈49 pkt/s) for a 3-minute session before overflowing.
        # Jitter buffer: don't start draining until TX_MIC_PRIME_PKTS are queued,
        # so bursty 20 ms WS frames can't underflow the 5.12 ms pacer (the cause
        # of the periodic clicking). Re-primes after any underflow.
        self._mic_primed = False
        self._first_prime = True          # first fill uses the high watermark
        self._mic_underruns = 0           # diagnostic counter
        # Continuous input→audio_rate resampler state (avoids per-frame
        # boundary discontinuities). _in_buf holds raw input-rate samples;
        # _rs_phase is the fractional read cursor into it.
        self._in_buf = np.array([], dtype=np.float32)
        self._rs_phase = 0.0
        self._in_rate = 16000
        # TX uses a flat drive-scaled make-up gain (TX_DRIVE_GAIN) — no mic AGC.
        # A tracking AGC here used to pump the envelope and make the far end
        # hear a trembling voice; it has been removed.
        # ── End-to-end level probes (1 Hz summary to server.log) ──
        # Captures RMS + peak at four stages so we can map mic level → final
        # IQ amplitude and identify the dominant gain/loss step. Reset each
        # PTT cycle via reset_mic(); server.py reads + flushes at 1 Hz.
        self._lvl_in_sum_sq = 0.0;     self._lvl_in_peak = 0.0;  self._lvl_in_n = 0
        self._lvl_an_sum_sq = 0.0;      self._lvl_an_peak = 0.0;  self._lvl_an_n = 0
        self._lvl_drv_sum_sq = 0.0;     self._lvl_drv_peak = 0.0; self._lvl_drv_n = 0
        self._lvl_lim_sum_sq = 0.0;     self._lvl_lim_peak = 0.0; self._lvl_lim_n = 0

    def set_drive(self, value: float):
        """Set TX drive 0..1 (linear scale below the TX_IQ_PEAK ceiling)."""
        self.drive = max(0.0, min(1.0, value))

    def _tx_level(self) -> float:
        """Current TX IQ peak target: verified ceiling × drive."""
        return TX_IQ_PEAK * self.drive

    def set_mode(self, mode: str):
        self.mode = mode.upper()
        # Tune IQ is pre-computed per mode; recompute if a WAV is loaded so a
        # mode change (e.g. USB→LSB) takes effect on the next tune playback.
        if len(self._tune_wav) > 0:
            self.set_tune_wav(self._tune_wav)

    def set_tune_wav(self, data: np.ndarray):
        """Pre-compute IQ-rate analytic signal for continuous TX playback.

        Hilbert at audio rate → linear interpolation of complex analytic
        signal to IQ rate.  No filter transients, phase-continuous.
        The ~0.5% AM ripple from linear interpolation is at the audio
        sample rate and inaudible after receiver filtering.
        """
        audio = np.asarray(data, dtype=np.float64)
        if len(audio) < 40:
            self._tune_iq = np.array([], dtype=np.complex64)
            return

        # Hilbert with internal zero-padding: transients land in the
        # zero-padded region, leaving the original-length output clean.
        pad = max(2048, len(audio) // 2)
        analytic_audio = hilbert(audio, N=len(audio) + pad * 2)[:len(audio)]
        # Sideband selection: USB = analytic, LSB = conjugate (mirror spectrum).
        # Without this, tune always transmitted USB regardless of mode.
        if self.mode == "LSB":
            analytic_audio = np.conj(analytic_audio)

        # Upsample analytic signal to TX IQ rate (×2.5 = 39063/15625) via
        # linear interpolation. Complex interpolation preserves analyticity.
        n_iq = int(round(len(audio) * self.up_ratio))
        xp = np.linspace(0, 1, len(audio))
        x  = np.linspace(0, 1, n_iq)
        up_i = np.interp(x, xp, np.real(analytic_audio))
        up_q = np.interp(x, xp, np.imag(analytic_audio))
        iq = (up_i + 1j * up_q).astype(np.complex128)

        # Cross-fade loop boundary: blend last ~200 samples into first ~200
        # to eliminate the Hilbert edge discontinuity when looping.
        fade_len = min(200, len(iq) // 10)
        fade = np.linspace(0, 1, fade_len, dtype=np.float64)
        iq[:fade_len] = iq[:fade_len] * (1 - fade) + iq[-fade_len:] * fade

        # Normalize to TX_IQ_PEAK, then scale by TX_TUNE_SCALE so the Tune
        # carrier transmits at a safe ~10 W (continuous constant-envelope tone
        # is far more PA thermal load than voice — see TX_TUNE_SCALE comment).
        peak = np.max(np.abs(iq))
        if peak > 1e-6:
            iq = (iq / peak) * TX_IQ_PEAK * TX_TUNE_SCALE
        self._tune_iq = iq.astype(np.complex64)
        self._tune_pos = 0
        self._tune_wav = np.asarray(data, dtype=np.float32)

    def activate_tune(self, active: bool):
        """Enable/disable tune playback mode."""
        self._tune_active = active
        if active:
            self._tune_pos = 0
            self._audio_buf = np.array([], dtype=np.float32)

    def feed_audio(self, pcm: bytes, input_rate: int = 16000) -> int:
        """Feed raw int16 PCM mic audio; push ready TX IQ packets to the queue.

        Pipeline (all phase-continuous across the bursty 20 ms WS frames):
          1. Continuous fractional resampler input_rate → audio_rate, with
             persistent input buffer + fractional read cursor — no per-frame
             reset, so frame seams introduce no discontinuity.
          2. Overlap-save Hilbert SSB in 80-sample (5.12 ms) hops, keeping
             MARGIN context each side so block edges stay clean.
          3. Each 80 audio → 200 IQ → one 1200-byte 0xFFFD packet pushed onto
             the thread-safe jitter-buffered queue the TX pacer drains.

        Returns the number of packets queued this call.
        """
        self._in_rate = input_rate
        x = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        if len(x) == 0:
            return 0

        # ── 1. Continuous fractional resampler input_rate → audio_rate ──
        self._in_buf = np.concatenate([self._in_buf, x])
        step = input_rate / self.audio_rate          # input samples per output
        out = []
        # Need one sample of right context for linear interpolation.
        while self._rs_phase < len(self._in_buf) - 1:
            i0 = int(self._rs_phase)
            frac = self._rs_phase - i0
            out.append(self._in_buf[i0] * (1.0 - frac) + self._in_buf[i0 + 1] * frac)
            self._rs_phase += step
        # Drop consumed input, keep the cursor fractional.
        consumed = int(self._rs_phase)
        if consumed > 0:
            self._in_buf = self._in_buf[consumed:]
            self._rs_phase -= consumed
        if not out:
            return 0
        self._audio_buf = np.concatenate(
            [self._audio_buf, np.asarray(out, dtype=np.float32)])

        # ── 2/3. Overlap-save SSB → IQ packets ──
        M = TX_HILBERT_MARGIN
        N = TX_AUDIO_PER_PKT
        ceiling = TX_IQ_PEAK                        # FIXED soft-limit knee (drive-independent)
        gain = TX_DRIVE_GAIN * self.drive          # flat make-up gain (no AGC)
        queued = 0
        while len(self._audio_buf) >= 2 * M + N:
            block = self._audio_buf[:2 * M + N]
            self._audio_buf = self._audio_buf[N:]   # advance one hop, keep overlap

            if self.mode == "LSB":
                analytic = np.conj(hilbert(block))
            elif self.mode == "USB":
                analytic = hilbert(block)
            elif self.mode == "AM":
                env = block - np.mean(block)
                analytic = env.astype(np.complex128)
            else:
                analytic = hilbert(block)

            center = analytic[M:M + N]                # 80 complex samples
            xp = np.linspace(0.0, 1.0, N)
            xq = np.linspace(0.0, 1.0, TX_PACKET_SAMPLES)
            iq = (np.interp(xq, xp, np.real(center))
                  + 1j * np.interp(xq, xp, np.imag(center)))

            # ── Flat make-up gain (NO AGC) ──
            # A fixed drive-scaled gain preserves the envelope, so the far end
            # hears a clean, steady voice. The per-hop tracking AGC that used
            # to live here was the source of the trembling/quivering audio.
            iq = iq * gain
            # Soft limiter (tanh) on magnitude — NOT a hard clip. A hard
            # magnitude clip puts a sharp corner on every syllable, which
            # generates wideband splatter across the whole passband (the
            # "mostly noise" the far end heard). tanh saturates smoothly toward
            # the ceiling, so transients round off instead of squaring off.
            mag = np.abs(iq)
            nz = mag > 1e-9
            iq_pre_lim = iq.copy()                 # probe: capture pre-tanh
            if np.any(nz):
                limited = ceiling * np.tanh(mag[nz] / ceiling)
                iq[nz] = iq[nz] / mag[nz] * limited
            iq = iq.astype(np.complex64)

            # ── End-to-end level probes (accumulate per-hop, log 1 Hz) ──
            # Four stages: input audio / analytic / post-drive / post-limiter.
            # All computed on the 200-sample packet for consistency. The input
            # and analytic probes are the pre-drive magnitude divided back
            # by `gain`, so we see the signal level at each stage.
            try:
                inv_g = 1.0 / gain if gain > 0 else 0.0
                in_mag = np.abs(np.real(iq_pre_lim)) * inv_g
                an_mag = np.abs(iq_pre_lim) * inv_g
                drv_mag = np.abs(iq_pre_lim)
                lim_mag = np.abs(iq).astype(np.float64)
                self._lvl_in_sum_sq  += float(np.sum(in_mag * in_mag))
                self._lvl_in_peak     = max(self._lvl_in_peak,  float(np.max(in_mag)))
                self._lvl_in_n       += len(in_mag)
                self._lvl_an_sum_sq  += float(np.sum(an_mag * an_mag))
                self._lvl_an_peak     = max(self._lvl_an_peak,  float(np.max(an_mag)))
                self._lvl_an_n       += len(an_mag)
                self._lvl_drv_sum_sq += float(np.sum(drv_mag * drv_mag))
                self._lvl_drv_peak    = max(self._lvl_drv_peak, float(np.max(drv_mag)))
                self._lvl_drv_n      += len(drv_mag)
                self._lvl_lim_sum_sq += float(np.sum(lim_mag * lim_mag))
                self._lvl_lim_peak    = max(self._lvl_lim_peak, float(np.max(lim_mag)))
                self._lvl_lim_n      += len(lim_mag)
            except Exception:
                pass

            pkt = self._encode_iq(iq)
            with self._mic_lock:
                self._mic_iq.append(pkt)
            queued += 1

        return queued

    def get_mic_iq(self) -> bytes | None:
        """Pop one queued mic TX IQ packet (1200 bytes), or None.

        Jitter-buffered with two-level hysteresis (see TX_MIC_PRIME_PKTS):
        stays un-primed (returns None → caller sends silence) until the high
        watermark TX_MIC_PRIME_PKTS is reached on first fill, then drains 1
        packet/call. On underflow it re-primes only to the lower
        TX_MIC_REPRIME_PKTS watermark, so a momentary WS stall costs a short
        re-buffer rather than a full prime-depth silence hole. Called from the
        TX pacer thread; thread-safe against feed_audio."""
        with self._mic_lock:
            if not self._mic_primed:
                # Use the high watermark for the very first fill, the lower
                # watermark for re-fills after an underflow.
                want = (TX_MIC_PRIME_PKTS if self._first_prime
                        else TX_MIC_REPRIME_PKTS)
                if len(self._mic_iq) >= want:
                    self._mic_primed = True
                    self._first_prime = False
                else:
                    return None                       # still filling — emit silence
            if self._mic_iq:
                return self._mic_iq.popleft()
            # Underflow: re-prime (to the cheaper low watermark) before resuming.
            self._mic_primed = False
            self._mic_underruns += 1
            return None

    def reset_mic(self):
        """Clear all mic state (call on PTT assert)."""
        self._audio_buf = np.array([], dtype=np.float32)
        self._in_buf = np.array([], dtype=np.float32)
        self._rs_phase = 0.0
        with self._mic_lock:
            self._mic_iq.clear()
            self._mic_primed = False
            self._first_prime = True      # PTT restart re-arms the deep prime
        # Reset the end-to-end level probe accumulators (PTT cycle boundary).
        self._lvl_in_sum_sq = 0.0;  self._lvl_in_peak = 0.0;  self._lvl_in_n = 0
        self._lvl_an_sum_sq = 0.0;  self._lvl_an_peak = 0.0;  self._lvl_an_n = 0
        self._lvl_drv_sum_sq = 0.0; self._lvl_drv_peak = 0.0; self._lvl_drv_n = 0
        self._lvl_lim_sum_sq = 0.0; self._lvl_lim_peak = 0.0; self._lvl_lim_n = 0

    def snapshot_levels(self) -> dict:
        """Return + reset the per-stage level accumulators. Called by server.py
        at 1 Hz to log end-to-end gain across the TX chain."""
        out = dict(
            in_sq=self._lvl_in_sum_sq, in_pk=self._lvl_in_peak, in_n=self._lvl_in_n,
            an_sq=self._lvl_an_sum_sq, an_pk=self._lvl_an_peak, an_n=self._lvl_an_n,
            drv_sq=self._lvl_drv_sum_sq, drv_pk=self._lvl_drv_peak, drv_n=self._lvl_drv_n,
            lim_sq=self._lvl_lim_sum_sq, lim_pk=self._lvl_lim_peak, lim_n=self._lvl_lim_n,
        )
        self._lvl_in_sum_sq = 0.0;  self._lvl_in_peak = 0.0;  self._lvl_in_n = 0
        self._lvl_an_sum_sq = 0.0;  self._lvl_an_peak = 0.0;  self._lvl_an_n = 0
        self._lvl_drv_sum_sq = 0.0; self._lvl_drv_peak = 0.0; self._lvl_drv_n = 0
        self._lvl_lim_sum_sq = 0.0; self._lvl_lim_peak = 0.0; self._lvl_lim_n = 0
        return out

    def get_tune_iq(self) -> bytes:
        """Get next 200 IQ samples from the pre-computed continuous analytic signal.

        Returns 1200 bytes (200 samples × 6 bytes 24-bit I/Q) or silence.
        """
        if len(self._tune_iq) == 0:
            return b'\x00' * 1200

        need = 200  # IQ samples per packet
        total = len(self._tune_iq)
        if self._tune_pos + need > total:
            chunk = np.concatenate([
                self._tune_iq[self._tune_pos:],
                self._tune_iq[:need - (total - self._tune_pos)]
            ])
            self._tune_pos = need - (total - self._tune_pos)
        else:
            chunk = self._tune_iq[self._tune_pos:self._tune_pos + need]
            self._tune_pos += need

        return self._encode_iq(chunk)

    def _modulate(self, audio: np.ndarray) -> np.ndarray | None:
        """TX_AUDIO_PER_PKT (80) audio samples → 200 IQ samples via Hilbert SSB."""
        if len(audio) < 16:
            return None

        # Upsample audio → 200 IQ samples (×2.5 @ 39063 Hz) via interpolation
        xp = np.linspace(0, 1, len(audio))
        x  = np.linspace(0, 1, 200)
        upsampled = np.interp(x, xp, audio).astype(np.float64)

        mode = self.mode
        if mode == "USB":
            analytic = hilbert(upsampled)
            iq = analytic.astype(np.complex128)
        elif mode == "LSB":
            analytic = hilbert(upsampled)
            iq = np.conj(analytic).astype(np.complex128)
        elif mode == "AM":
            envelope = np.abs(upsampled)
            envelope -= np.mean(envelope)
            iq = envelope.astype(np.complex128)
        elif mode in ("FM", "NFM"):
            phase = np.cumsum(upsampled) * TX_IQ_PEAK / 200.0
            iq = TX_IQ_PEAK * np.exp(1j * phase)
        elif mode == "CW":
            t = np.arange(200, dtype=np.float64) / self.iq_rate
            iq = TX_IQ_PEAK * np.exp(2j * np.pi * 700.0 * t)
        else:
            return None

        # Normalize to verified TX ceiling (~0.09 peak, firmware has no ALC)
        peak = np.max(np.abs(iq))
        if peak > 1e-6:
            iq = (iq / peak) * TX_IQ_PEAK

        return iq.astype(np.complex64)

    def reset_tx_ramp(self):
        """Re-arm the TX amplitude ramp. Call on PTT assert so the first
        TX_RAMP_SAMPLES after real modulation begins are scaled 0→1, removing
        the hard step from the zero-IQ settling pad to full-amplitude IQ."""
        self._ramp_pos = 0

    def _apply_ramp(self, iq: np.ndarray) -> np.ndarray:
        """Scale the leading edge of TX IQ by a linear 0→1 gain ramp until
        TX_RAMP_SAMPLES have been emitted. No-op once fully ramped."""
        n = len(iq)
        if self._ramp_pos >= TX_RAMP_SAMPLES or n == 0:
            return iq
        idx = self._ramp_pos + np.arange(n)
        gain = np.clip(idx / float(TX_RAMP_SAMPLES), 0.0, 1.0).astype(np.float32)
        self._ramp_pos += n
        return (iq * gain).astype(np.complex64)

    def _encode_iq(self, iq: np.ndarray) -> bytes:
        """Pack 200 IQ samples as 24-bit LE bytes (1200 bytes total)."""
        iq = self._apply_ramp(iq)
        return _pack_iq_24bit(iq)

    def generate_test_tone(self, freq_hz: float = 700.0,
                           duration_samples: int = 200) -> np.ndarray:
        """Generate a pure-tone USB test signal (for TX verification)."""
        t = np.arange(duration_samples) / self.iq_rate + self._phase
        self._phase = t[-1] + 1.0 / self.iq_rate
        iq = TX_IQ_PEAK * np.exp(2j * np.pi * freq_hz * t).astype(np.complex64)
        return iq


def encode_tx_iq_packet(iq: np.ndarray) -> bytes:
    """Encode 200 IQ samples as 24-bit LE raw bytes for TX stream."""
    return _pack_iq_24bit(iq)


# ── Stream Processor ───────────────────────────────────────────────

@dataclass
class StreamProcessor:
    spectrum: SpectrumProcessor
    demodulator: AudioDemodulator
    modulator: TXModulator = None
    latest_spectrum: list | None = None
    audio_chunks: deque = None

    def __post_init__(self):
        self.audio_chunks = deque(maxlen=100)
        if self.modulator is None:
            self.modulator = TXModulator()

    def feed_iq(self, iq: np.ndarray):
        spec = self.spectrum.feed(iq)
        if spec is not None:
            self.latest_spectrum = self.spectrum.bin(spec, 512)
        audio = self.demodulator.demodulate(iq)
        if audio is not None and len(audio) > 0:
            self.demodulator.audio_buffer.extend(audio.tolist())
            chunk = self.demodulator.get_audio_chunk()
            if chunk: self.audio_chunks.append(chunk)

    def set_iq_sample_rate(self, hz: int):
        """Update IQ sample rate for the demodulator + global state."""
        set_iq_sample_rate(hz)
        self.demodulator.set_sample_rate(hz)

    def get_audio(self) -> bytes | None:
        return self.audio_chunks.popleft() if self.audio_chunks else None

    def get_tx_iq(self) -> bytes | None:
        """Get one TX IQ packet. Priority: tune_WAV > mic audio > silence.

        Returns 1200 bytes or None (caller should send silence if None).
        Note: there is intentionally no 700 Hz test-tone fallback — PTT with
        no mic audio queued must emit silence, not a carrier tone.
        """
        if self.modulator is None:
            return None
        # 1. Tune mode: play pre-computed IQ signal (continuous carrier/WAV)
        if self.modulator._tune_active and len(self.modulator._tune_iq) > 0:
            return self.modulator.get_tune_iq()
        # 2. Live mic audio queued by feed_audio() on the WS thread
        mic = self.modulator.get_mic_iq()
        if mic is not None:
            return mic
        # 3. No audio ready → silence (keeps the TX stream paced)
        return b'\x00' * 1200

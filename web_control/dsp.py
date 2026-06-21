"""
SunSDR2 DX IQ Stream Processor
===============================
IQ → IF shift → complex SSB bandpass → real extraction → decimation → AGC → audio.

True IQ sample rate: 78125 Hz (5^7)
RX DDS = VFO + 30500 Hz IF offset
Audio output: 15625 Hz (5× decimation)
"""

import math, logging, struct
import numpy as np
from collections import deque
from dataclasses import dataclass
from scipy.signal import hilbert

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────

IQ_SAMPLE_RATE = 78125      # verified: 390.7 pkt/s × 200 samples
IF_OFFSET = 30500.0         # RX DDS - TX VFO (verified from pcap)
AUDIO_DECIM = 5
AUDIO_RATE = IQ_SAMPLE_RATE // AUDIO_DECIM   # 15625 Hz
FFT_SIZE = 2048

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
        self._wdsp_enabled = False
        self._nr2_enabled = False
        self._nr2_level = 50
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
            logger.info("WDSP ready (AGC SLOW + NR2)")
        except Exception as e:
            logger.debug(f"WDSP unavailable: {e}")

    # ── Control ────────────────────────────────────────────────

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

        # Built-in AGC
        if len(audio) > 0:
            rms = float(np.sqrt(np.mean(audio**2) + 1e-12))
            if self._agc_gain is None:
                self._agc_gain = 0.25 / rms if rms > 1e-10 else 1.0
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

class TXModulator:
    """Audio → SSB IQ: upsampling + Hilbert analytic signal → 24-bit IQ packets.

    Output matches the TX stream format (sub=0xFFFD, 200 samples × 6 bytes).
    Supports USB, LSB, AM, FM, CW modes.
    """

    def __init__(self, audio_rate: int = AUDIO_RATE, iq_rate: int = IQ_SAMPLE_RATE):
        self.audio_rate = audio_rate       # 15625 Hz
        self.iq_rate = iq_rate             # 78125 Hz
        self.mode = "USB"
        self._audio_buf = np.array([], dtype=np.float32)
        self._phase = 0.0
        # Tune mode state
        self._tune_active = False
        self._tune_wav = np.array([], dtype=np.float32)  # pre-loaded WAV at audio_rate
        self._tune_iq = np.array([], dtype=np.complex64)  # pre-computed IQ-rate analytic signal
        self._tune_pos = 0

    def set_mode(self, mode: str):
        self.mode = mode.upper()

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

        # Upsample analytic signal to IQ rate (×5) via linear interpolation.
        # Complex interpolation preserves the analytic property.
        n_iq = len(audio) * 5
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

        # Normalize to 30% modulation
        peak = np.max(np.abs(iq))
        if peak > 1e-6:
            iq = (iq / peak) * 0.3
        self._tune_iq = iq.astype(np.complex64)
        self._tune_pos = 0
        self._tune_wav = np.asarray(data, dtype=np.float32)

    def activate_tune(self, active: bool):
        """Enable/disable tune playback mode."""
        self._tune_active = active
        if active:
            self._tune_pos = 0
            self._audio_buf = np.array([], dtype=np.float32)

    def feed_audio(self, pcm: bytes, input_rate: int = 16000) -> bytes | None:
        """Feed raw int16 PCM audio, returns 24-bit IQ packet bytes or None.

        Audio is resampled from input_rate (default 16000) to self.audio_rate (15625).
        """
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        self._audio_buf = np.concatenate([self._audio_buf, audio])

        need = max(40, int(40 * input_rate / self.audio_rate))
        if len(self._audio_buf) < need:
            return None

        chunk = self._audio_buf[:need]
        self._audio_buf = self._audio_buf[need:]

        if input_rate != self.audio_rate:
            t_in = np.linspace(0, 1, len(chunk))
            t_out = np.linspace(0, 1, 40)
            chunk = np.interp(t_out, t_in, chunk).astype(np.float32)

        iq = self._modulate(chunk)
        if iq is None:
            return None

        return self._encode_iq(iq)

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
        """40 audio samples → 200 IQ samples via upsampling + Hilbert SSB."""
        if len(audio) < 16:
            return None

        # Upsample: 40 audio samples → 200 IQ samples via linear interpolation
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
            phase = np.cumsum(upsampled) * 0.3 / 200.0
            iq = 0.3 * np.exp(1j * phase)
        elif mode == "CW":
            t = np.arange(200, dtype=np.float64) / self.iq_rate
            iq = 0.3 * np.exp(2j * np.pi * 700.0 * t)
        else:
            return None

        # Normalize to 30% modulation
        peak = np.max(np.abs(iq))
        if peak > 1e-6:
            iq = (iq / peak) * 0.3

        return iq.astype(np.complex64)

    def _encode_iq(self, iq: np.ndarray) -> bytes:
        """Pack 200 IQ samples as 24-bit LE bytes (1200 bytes total)."""
        raw = b''
        for i in range(len(iq)):
            iv = int(np.clip(np.real(iq[i]) * 8388608, -8388608, 8388607))
            qv = int(np.clip(np.imag(iq[i]) * 8388608, -8388608, 8388607))
            raw += struct.pack('<i', iv)[:3]
            raw += struct.pack('<i', qv)[:3]
        return raw

    def generate_test_tone(self, freq_hz: float = 700.0,
                           duration_samples: int = 200) -> np.ndarray:
        """Generate a pure-tone USB test signal (for TX verification)."""
        t = np.arange(duration_samples) / self.iq_rate + self._phase
        self._phase = t[-1] + 1.0 / self.iq_rate
        iq = 0.3 * np.exp(2j * np.pi * freq_hz * t).astype(np.complex64)
        return iq


def encode_tx_iq_packet(iq: np.ndarray) -> bytes:
    """Encode 200 IQ samples as 24-bit LE raw bytes for TX stream."""
    raw = b''
    for i in range(len(iq)):
        iv = int(np.clip(np.real(iq[i]) * 8388608, -8388608, 8388607))
        qv = int(np.clip(np.imag(iq[i]) * 8388608, -8388608, 8388607))
        raw += struct.pack('<i', iv)[:3]
        raw += struct.pack('<i', qv)[:3]
    return raw


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

    def get_audio(self) -> bytes | None:
        return self.audio_chunks.popleft() if self.audio_chunks else None

    def get_tx_iq(self) -> bytes | None:
        """Get one TX IQ packet. Priority: tune_WAV > test_tone > silence.

        Returns 1200 bytes or None (caller should send silence if None).
        """
        if self.modulator is None:
            return None
        # 1. Tune mode: play pre-computed IQ signal
        if self.modulator._tune_active and len(self.modulator._tune_iq) > 0:
            return self.modulator.get_tune_iq()
        # 2. Fallback: test tone for verification
        iq = self.modulator.generate_test_tone(700.0, 200)
        return encode_tx_iq_packet(iq)

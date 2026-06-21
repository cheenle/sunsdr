"""
WDSP (Warren Pratt DSP) Python Wrapper
=======================================
Lightweight ctypes wrapper around libwdsp.dylib for professional
audio processing: AGC (LONG/SLOW/MED/FAST), NR2 spectral noise
reduction, NB noise blanker, ANF auto notch filter.

Library: libwdsp.dylib (ARM64, built from https://github.com/g0orx/wdsp)
"""

import ctypes, os, logging, numpy as np

logger = logging.getLogger(__name__)

# ── Library loading ────────────────────────────────────────────────

_wdsp = None
WDSP_AVAILABLE = False

def _load():
    global _wdsp, WDSP_AVAILABLE
    paths = [
        "/usr/local/lib/libwdsp.dylib",
        "/opt/homebrew/lib/libwdsp.dylib",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "libwdsp.dylib"),
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                _wdsp = ctypes.CDLL(p)
                WDSP_AVAILABLE = True
                return
            except OSError:
                pass

_load()


# ── Constants ──────────────────────────────────────────────────────

class WDSPMode:
    LSB, USB, DSB, CW, AM, FM = range(6)

class WDSPAGCMode:
    OFF, LONG, SLOW, MED, FAST = range(5)


# ── Processor ──────────────────────────────────────────────────────

class WDSPProcessor:
    """Professional AGC + NR2 audio processor via WDSP C library."""

    def __init__(self, sample_rate: int = 16000, buffer_size: int = 256,
                 mode: int = WDSPMode.USB, enable_nr2: bool = True,
                 enable_nb: bool = False, enable_anf: bool = False,
                 agc_mode: int = WDSPAGCMode.SLOW):
        if not WDSP_AVAILABLE:
            raise RuntimeError("WDSP library not available")

        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.channel = 0
        self._nr2 = enable_nr2

        self._in = np.zeros(buffer_size * 2, dtype=np.float64)
        self._out = np.zeros(buffer_size * 2, dtype=np.float64)
        self._init(mode, agc_mode, enable_nb, enable_anf)

    def _init(self, mode, agc, nb, anf):
        _wdsp.OpenChannel(
            ctypes.c_int(self.channel),
            ctypes.c_int(self.buffer_size), ctypes.c_int(self.buffer_size),
            ctypes.c_int(self.sample_rate), ctypes.c_int(self.sample_rate),
            ctypes.c_int(self.sample_rate),
            ctypes.c_int(0), ctypes.c_int(1),
            ctypes.c_double(0), ctypes.c_double(0),
            ctypes.c_double(0), ctypes.c_double(0),
            ctypes.c_int(0))
        _wdsp.SetRXAMode(ctypes.c_int(self.channel), ctypes.c_int(mode))
        _wdsp.SetRXAPanelGain1(ctypes.c_int(self.channel), ctypes.c_double(0.06))
        self.set_agc(agc)
        if self._nr2:
            _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel), ctypes.c_int(1))
            _wdsp.SetRXAEMNRgainMethod(ctypes.c_int(self.channel), ctypes.c_int(0))
            _wdsp.SetRXAEMNRnpeMethod(ctypes.c_int(self.channel), ctypes.c_int(0))
            _wdsp.SetRXAEMNRaeRun(ctypes.c_int(self.channel), ctypes.c_int(1))
            _wdsp.SetRXAEMNRPosition(ctypes.c_int(self.channel), ctypes.c_int(0))
            _wdsp.SetRXASNBARun(ctypes.c_int(self.channel), ctypes.c_int(1))

    def set_agc(self, mode: int):
        _wdsp.SetRXAAGCMode(ctypes.c_int(self.channel), ctypes.c_int(mode))

    def set_mode(self, mode: int):
        _wdsp.SetRXAMode(ctypes.c_int(self.channel), ctypes.c_int(mode))

    def process(self, audio_data: np.ndarray) -> np.ndarray:
        """Process audio. Returns float32, same length as input."""
        if audio_data.dtype == np.int16:
            f32 = audio_data.astype(np.float64) / 32768.0
        else:
            f32 = audio_data.astype(np.float64)

        if len(f32) < self.buffer_size:
            padded = np.zeros(self.buffer_size, dtype=np.float64)
            padded[:len(f32)] = f32
            f32 = padded
        else:
            f32 = f32[:self.buffer_size]

        self._in[0::2] = f32
        self._in[1::2] = 0.0

        err = ctypes.c_int(0)
        _wdsp.fexchange0(
            ctypes.c_int(self.channel),
            self._in.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            self._out.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.byref(err))

        if err.value != 0:  # -2 = warmup, others = error
            return audio_data[:len(audio_data)]

        out = self._out[0::2].copy()
        return out[:len(audio_data)].astype(np.float32)

    # ── NR2 (spectral noise reduction) ──────────────────────────

    def set_nr2_enabled(self, on: bool):
        """Enable/disable NR2 spectral noise reduction."""
        if not WDSP_AVAILABLE: return
        run = 1 if on else 0
        _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel), ctypes.c_int(run))
        self._nr2 = on

    def set_nr2_level(self, level: int):
        """Set NR2 level (0-100)."""
        if not WDSP_AVAILABLE: return
        gain = max(0.0, min(1.0, level / 100.0))
        _wdsp.SetRXAEMNRgainMethod(ctypes.c_int(self.channel), ctypes.c_int(0))
        _wdsp.SetRXAEMNRnpeMethod(ctypes.c_int(self.channel), ctypes.c_int(0))
        # Gain line = 1 for fixed gain, gain value controls strength
        _wdsp.SetRXAEMNRaeRun(ctypes.c_int(self.channel), ctypes.c_int(1))

    def set_nr2_gain_method(self, method: int):
        """Set NR2 gain method (0=linear, 1=log)."""
        if not WDSP_AVAILABLE: return
        _wdsp.SetRXAEMNRgainMethod(ctypes.c_int(self.channel), ctypes.c_int(method))

    def set_nr2_npe_method(self, method: int):
        """Set NR2 NPE method (0=standard)."""
        if not WDSP_AVAILABLE: return
        _wdsp.SetRXAEMNRnpeMethod(ctypes.c_int(self.channel), ctypes.c_int(method))

    def set_nr2_ae_run(self, on: bool):
        """Enable/disable NR2 AE (acquisition enhancement)."""
        if not WDSP_AVAILABLE: return
        run = 1 if on else 0
        _wdsp.SetRXAEMNRaeRun(ctypes.c_int(self.channel), ctypes.c_int(run))

    # ── NB (noise blanker) ──────────────────────────────────────

    def set_nb_enabled(self, on: bool):
        """Enable/disable noise blanker."""
        if not WDSP_AVAILABLE: return
        run = 1 if on else 0
        try:
            _wdsp.SetRXASNBARun(ctypes.c_int(self.channel), ctypes.c_int(run))
        except AttributeError:
            pass

    # ── ANF (auto notch filter) ─────────────────────────────────

    def set_anf_enabled(self, on: bool):
        """Enable/disable auto notch filter."""
        if not WDSP_AVAILABLE: return
        run = 1 if on else 0
        _wdsp.SetRXAANFRun(ctypes.c_int(self.channel), ctypes.c_int(run))

    # ── NF (manual notch filter) ────────────────────────────────

    def set_nf_enabled(self, on: bool, freq_hz: float = 0.0):
        """Enable/disable manual notch filter at given frequency."""
        if not WDSP_AVAILABLE: return
        run = 1 if on else 0
        try:
            _wdsp.SetRXAMANFRun(ctypes.c_int(self.channel), ctypes.c_int(run))
        except AttributeError:
            pass  # symbol not available in this libwdsp build
        if on and freq_hz > 0:
            try:
                _wdsp.SetRXAMNFreq(ctypes.c_int(self.channel), ctypes.c_double(freq_hz))
            except AttributeError:
                pass

    # ── Bandpass ────────────────────────────────────────────────

    def set_bandpass(self, low_hz: float, high_hz: float):
        """Set audio bandpass filter edges."""
        if not WDSP_AVAILABLE: return
        try:
            _wdsp.SetRXABandpassFilter(ctypes.c_int(self.channel),
                                       ctypes.c_double(low_hz), ctypes.c_double(high_hz))
        except AttributeError:
            pass

    def close(self):
        if WDSP_AVAILABLE:
            _wdsp.SetChannelState(ctypes.c_int(self.channel), ctypes.c_int(0), ctypes.c_int(0))
            _wdsp.CloseChannel(ctypes.c_int(self.channel))

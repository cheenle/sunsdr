"""
WDSP (Warren Pratt DSP) Python Wrapper
=======================================
Lightweight ctypes wrapper around libwdsp.dylib for professional
audio processing: AGC (LONG/SLOW/MED/FAST), NR2 spectral noise
reduction, NB noise blanker, ANF auto notch filter.

Library: libwdsp.dylib (ARM64, built from https://github.com/g0orx/wdsp)

Fixed 2026-06-24:
  - process() now buffers input → chunked 256-sample blocks → concatenates
    output, so variable-length input is fully processed (no truncation).
  - set_nr2_level() now calls SetRXAEMNRgainLine to actually set NR2 strength.
  - Warmup output (err=-2) is no longer discarded.
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
        self._nr2_level = 50

        self._in = np.zeros(buffer_size * 2, dtype=np.float64)
        self._out = np.zeros(buffer_size * 2, dtype=np.float64)
        # Streaming buffers — process() accumulates variable-length input
        # into _input_buffer, drains in buffer_size chunks through
        # fexchange0, and queues output into _output_buffer.  Output is
        # returned FIFO so the caller always gets time-aligned samples.
        self._input_buffer = np.array([], dtype=np.float64)
        self._output_buffer = np.array([], dtype=np.float64)
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
        # Panel gain: 0.06 was too low, 0.5 over-drives the chain.
        # 0.2 gives NR2 a workable input level without saturating WDSP.
        # WDSP AGC (SLOW/MED/FAST) handles final output level.
        _wdsp.SetRXAPanelGain1(ctypes.c_int(self.channel), ctypes.c_double(0.2))
        self.set_agc(agc)
        if self._nr2:
            _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel), ctypes.c_int(1))
            _wdsp.SetRXAEMNRgainMethod(ctypes.c_int(self.channel), ctypes.c_int(0))
            _wdsp.SetRXAEMNRnpeMethod(ctypes.c_int(self.channel), ctypes.c_int(0))
            _wdsp.SetRXAEMNRaeRun(ctypes.c_int(self.channel), ctypes.c_int(1))
            _wdsp.SetRXAEMNRPosition(ctypes.c_int(self.channel), ctypes.c_int(0))
            _wdsp.SetRXASNBARun(ctypes.c_int(self.channel), ctypes.c_int(1))
            # Apply the default NR2 level via the gain-line API.
            self._apply_nr2_gain_line(self._nr2_level)
        if nb:
            self.set_nb_enabled(True)
        if anf:
            self.set_anf_enabled(True)

    # ── NR2 gain-line helper ────────────────────────────────────
    def _apply_nr2_gain_line(self, level: int):
        """Set NR2 gain-line from 0-100 level.

        WDSP gain-line range is 0.0–1.0.  A value of 0.0 means maximum NR2
        (aggressive noise suppression); 1.0 means minimum.  We invert the
        frontend's 0–100 slider so 100 = most aggressive, 0 = off.
        """
        try:
            gain = 1.0 - max(0.0, min(1.0, level / 100.0))
            _wdsp.SetRXAEMNRgainLine(
                ctypes.c_int(self.channel), ctypes.c_double(gain))
        except AttributeError:
            pass  # symbol not available in this libwdsp build

    def set_agc(self, mode: int):
        _wdsp.SetRXAAGCMode(ctypes.c_int(self.channel), ctypes.c_int(mode))

    def set_mode(self, mode: int):
        _wdsp.SetRXAMode(ctypes.c_int(self.channel), ctypes.c_int(mode))

    def process(self, audio_data: np.ndarray) -> np.ndarray:
        """Process audio through the WDSP chain.

        Handles variable-length input by buffering and draining in
        buffer_size blocks through fexchange0.  Returns float32 output
        with exactly the same number of samples as the input, properly
        time-aligned via a FIFO output buffer.
        """
        # Convert to float64
        if audio_data.dtype == np.int16:
            f32 = audio_data.astype(np.float64) / 32768.0
        else:
            f32 = audio_data.astype(np.float64)

        n_in = len(f32)
        if n_in == 0:
            return audio_data

        # Accumulate input and drain complete blocks
        self._input_buffer = np.concatenate([self._input_buffer, f32])
        bs = self.buffer_size

        while len(self._input_buffer) >= bs:
            chunk = self._input_buffer[:bs]
            self._input_buffer = self._input_buffer[bs:]

            self._in[0::2] = chunk
            self._in[1::2] = 0.0

            err = ctypes.c_int(0)
            _wdsp.fexchange0(
                ctypes.c_int(self.channel),
                self._in.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                self._out.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                ctypes.byref(err))

            # err == 0: normal
            # err == -2: warmup (output is valid but not fully converged)
            # err == other: real error → skip this block
            if err.value not in (0, -2):
                logger.debug("WDSP fexchange0 error=%d (skipping block)", err.value)
                continue

            out = self._out[0::2].copy()
            self._output_buffer = np.concatenate([self._output_buffer, out])

        # Return exactly n_in time-aligned samples
        if len(self._output_buffer) >= n_in:
            result = self._output_buffer[:n_in]
            self._output_buffer = self._output_buffer[n_in:]
            return result.astype(np.float32)

        # Not enough output yet — drain what we have, pad with silence
        if len(self._output_buffer) > 0:
            result = np.concatenate([
                self._output_buffer,
                np.zeros(n_in - len(self._output_buffer), dtype=np.float64),
            ])
            self._output_buffer = np.array([], dtype=np.float64)
            return result.astype(np.float32)

        # No output at all — return silence
        return np.zeros(n_in, dtype=np.float32)

    # ── NR2 (spectral noise reduction) ──────────────────────────

    def set_nr2_enabled(self, on: bool):
        """Enable/disable NR2 spectral noise reduction."""
        if not WDSP_AVAILABLE: return
        run = 1 if on else 0
        _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel), ctypes.c_int(run))
        self._nr2 = on

    def set_nr2_level(self, level: int):
        """Set NR2 strength (0-100). 100 = most aggressive."""
        if not WDSP_AVAILABLE: return
        self._nr2_level = max(0, min(100, level))
        self._apply_nr2_gain_line(self._nr2_level)

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

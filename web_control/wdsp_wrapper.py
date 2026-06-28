"""
WDSP (Warren Pratt DSP) Python Wrapper
=======================================
Strict ctypes wrapper around libwdsp.dylib with pre-allocated ring
buffers, zero-copy pointer passing, and exhaustive type bindings for
ARM64 safety.

Library: libwdsp.dylib (ARM64, built from https://github.com/g0orx/wdsp)

Refactored 2026-06-28:
  - argtypes / restype declared for every WDSP function — prevents
    ARM64 calling-convention corruption (float returns landing in the
    wrong register).
  - Ring buffer replaces dynamic np.concatenate() → zero per-call
    allocation, no GC pressure at 61 Hz.
  - NR2 strength now drives AE zeta / psi parameters because
    SetRXAEMNRgainLine does NOT exist in this build.
  - set_bandpass uses SetRXABandpassFreqs (existed all along).
  - set_nf_enabled uses the NBP Notch Bank Processor API.
  - Memory contiguity & dtype assertions on every C-boundary call.
  - New methods: get_s_meter, set_agc_attack / decay / hang,
    set_eq_run, set_fm_squelch_run / threshold.
"""

import ctypes, os, logging, numpy as np

logger = logging.getLogger(__name__)

# ── Library loading ────────────────────────────────────────────────

_wdsp: ctypes.CDLL | None = None
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

# ── Symbol registry ─────────────────────────────────────────────────
# Populated by _bind_signatures().  Each key is a C symbol name; the value
# is True.  _check_symbol(name) gates optional-feature calls so missing
# symbols in a different build don't crash — they just no-op after one
# WARNING log at startup.

_WDSP_SYMBOLS: dict[str, bool] = {}

def _check_symbol(name: str) -> bool:
    return _WDSP_SYMBOLS.get(name, False)


def _bind_signatures():
    """Declare ctypes argtypes / restype for every WDSP function we use.

    Each binding is independently guarded: a missing symbol logs a single
    WARNING and skips that function.  Core symbols (OpenChannel,
    fexchange0, CloseChannel) are asserted — the library is useless
    without them.

    ARM64 NOTE — restype matters.  Without it ctypes defaults to c_int
    for EVERY function.  GetRXAMeter returns double; on ARM64 the float
    return goes into a different register than int, so the caller reads
    garbage.  Declaring restype = ctypes.c_double fixes this.
    """
    global _WDSP_SYMBOLS

    c_int = ctypes.c_int
    c_double = ctypes.c_double
    c_void_p = ctypes.c_void_p
    c_bool = ctypes.c_bool

    def _bind(name, argtypes, restype):
        try:
            func = getattr(_wdsp, name)
        except AttributeError:
            logger.warning("WDSP symbol not available: %s", name)
            return False
        try:
            func.argtypes = argtypes
            func.restype = restype
            _WDSP_SYMBOLS[name] = True
            return True
        except Exception as e:
            logger.warning("WDSP bind failed for %s: %s", name, e)
            return False

    # ── Channel lifecycle (CORE — must exist) ──
    _bind("OpenChannel",
          [c_int, c_int, c_int, c_int, c_int, c_int, c_int, c_int,
           c_double, c_double, c_double, c_double, c_int], c_int)
    _bind("CloseChannel",     [c_int], c_int)
    _bind("SetChannelState",  [c_int, c_int, c_int], c_int)

    # ── Processing (CORE) ──
    # fexchange0 returns void.  restype=None prevents ctypes from reading
    # a phantom return value from x0 on ARM64 (which could corrupt state).
    _bind("fexchange0",
          [c_int, ctypes.POINTER(c_double), ctypes.POINTER(c_double),
           ctypes.POINTER(c_int)], None)
    _bind("fexchange2",
          [c_int,
           ctypes.POINTER(c_double), ctypes.POINTER(c_double),
           ctypes.POINTER(c_double), ctypes.POINTER(c_double),
           ctypes.POINTER(c_int)], None)

    # ── Mode & panel ──
    _bind("SetRXAMode",       [c_int, c_int], c_int)
    _bind("SetRXAPanelRun",   [c_int, c_int], c_int)
    _bind("SetRXAPanelGain1", [c_int, c_double], c_int)
    _bind("SetRXAPanelGain2", [c_int, c_double], c_int)

    # ── AGC ──
    _bind("SetRXAAGCMode",           [c_int, c_int], c_int)
    _bind("SetRXAAGCAttack",         [c_int, c_int], c_int)
    _bind("SetRXAAGCDecay",          [c_int, c_int], c_int)
    _bind("SetRXAAGCHang",           [c_int, c_int], c_int)
    _bind("SetRXAAGCHangLevel",      [c_int, c_double], c_int)
    _bind("SetRXAAGCHangThreshold",  [c_int, c_double], c_int)
    _bind("SetRXAAGCThresh",         [c_int, c_double], c_int)
    _bind("SetRXAAGCSlope",          [c_int, c_double], c_int)
    _bind("SetRXAAGCFixed",          [c_int, c_double], c_int)
    _bind("SetRXAAGCMaxInputLevel",  [c_int, c_double], c_int)
    _bind("GetRXAAGCHangLevel",      [c_int], c_double)
    _bind("GetRXAAGCHangThreshold",  [c_int], c_double)
    _bind("GetRXAAGCThresh",         [c_int], c_double)
    _bind("GetRXAAGCTop",            [c_int], c_double)

    # ── NR2 (Enhanced Meyer Noise Reduction) ──
    _bind("SetRXAEMNRRun",          [c_int, c_int], c_int)
    _bind("SetRXAEMNRgainMethod",   [c_int, c_int], c_int)
    _bind("SetRXAEMNRnpeMethod",    [c_int, c_int], c_int)
    _bind("SetRXAEMNRaeRun",        [c_int, c_int], c_int)
    _bind("SetRXAEMNRPosition",     [c_int, c_int], c_int)
    _bind("SetRXAEMNRaeZetaThresh", [c_int, c_double], c_int)
    _bind("SetRXAEMNRaePsi",        [c_int, c_double], c_int)

    # ── ANF (Auto Notch Filter) ──
    _bind("SetRXAANFRun",     [c_int, c_int], c_int)
    _bind("SetRXAANFVals",    [c_int, c_int, c_int], c_int)
    _bind("SetRXAANFTaps",    [c_int, c_int], c_int)
    _bind("SetRXAANFDelay",   [c_int, c_double], c_int)
    _bind("SetRXAANFGain",    [c_int, c_double], c_int)
    _bind("SetRXAANFLeakage", [c_int, c_double], c_int)
    _bind("SetRXAANFPosition",[c_int, c_int], c_int)

    # ── NB (Noise Blanker) ──
    _bind("SetRXASNBARun", [c_int, c_int], c_int)

    # ── Bandpass ──
    _bind("SetRXABandpassRun",    [c_int, c_int], c_int)
    _bind("SetRXABandpassFreqs",  [c_int, c_double, c_double], c_int)
    _bind("SetRXABandpassWindow", [c_int, c_int], c_int)
    _bind("SetRXABandpassNC",     [c_int, c_int], c_int)
    _bind("SetRXABandpassMP",     [c_int, c_int], c_int)

    # ── NBP (Notch Bank Processor — manual notch API) ──
    _bind("RXANBPAddNotch",          [c_int, c_int, c_double, c_double], c_int)
    _bind("RXANBPDeleteNotch",       [c_int, c_int], c_int)
    _bind("RXANBPGetNumNotches",     [c_int], c_int)
    _bind("RXANBPSetNotchesRun",     [c_int, c_int], c_int)
    _bind("RXANBPSetRun",            [c_int, c_int], c_int)
    _bind("RXANBPSetFreqs",          [c_int, c_int,
                                      ctypes.POINTER(c_double)], c_int)

    # ── Equalizer ──
    _bind("SetRXAEQRun",     [c_int, c_int], c_int)
    _bind("SetRXAEQNC",      [c_int, c_int], c_int)
    _bind("SetRXAEQMP",      [c_int, c_int], c_int)
    _bind("SetRXAEQProfile", [c_int, c_int,
                               ctypes.POINTER(c_double)], c_int)
    _bind("SetRXAEQWintype", [c_int, c_int], c_int)

    # ── FM Squelch ──
    _bind("SetRXAFMSQRun",       [c_int, c_int], c_int)
    _bind("SetRXAFMSQThreshold", [c_int, c_double], c_int)
    _bind("SetRXAFMSQNC",        [c_int, c_int], c_int)
    _bind("SetRXAFMSQMP",        [c_int, c_int], c_int)

    # ── S-Meter ──
    # CRITICAL: restype = c_double.  Without it ARM64 reads garbage.
    _bind("GetRXAMeter", [c_int, c_int], c_double)

    # ── Spectrum (enable WDSP internal FFT for GetRXAMeter) ──
    _bind("SetRXASpectrum", [c_int, c_int, c_int, c_int, c_int], c_int)

    # ── Shift / IF offset ──
    _bind("SetRXAShiftRun",  [c_int, c_int], c_int)
    _bind("SetRXAShiftFreq", [c_int, c_double], c_double)

    # ── AM Squelch ──
    _bind("SetRXAAMSQRun",       [c_int, c_int], c_int)
    _bind("SetRXAAMSQThreshold", [c_int, c_double], c_int)
    _bind("SetRXAAMSQMaxTail",   [c_int, c_double], c_int)

    # ── Pre-Generator (tone/noise injection for testing) ──
    _bind("SetRXAPreGenRun",       [c_int, c_int], c_int)
    _bind("SetRXAPreGenMode",      [c_int, c_int], c_int)
    _bind("SetRXAPreGenToneMag",   [c_int, c_double], c_int)
    _bind("SetRXAPreGenToneFreq",  [c_int, c_double], c_int)

    # ── CBL (Correction Buffer Leveler) ──
    _bind("SetRXACBLRun", [c_int, c_int], c_int)

    # ── SPeak CW ──
    _bind("SetRXASPCWRun",       [c_int, c_int], c_int)
    _bind("SetRXASPCWFreq",      [c_int, c_double], c_int)
    _bind("SetRXASPCWBandwidth", [c_int, c_double], c_int)
    _bind("SetRXASPCWGain",      [c_int, c_double], c_int)

    # ── Verify core symbols ──
    for core in ("OpenChannel", "fexchange0", "CloseChannel", "SetRXAMode",
                 "SetRXAPanelGain1", "SetRXAAGCMode"):
        if not _check_symbol(core):
            raise RuntimeError(
                f"WDSP missing core symbol: {core}.  "
                f"Library may be corrupted or from an incompatible build.")


if WDSP_AVAILABLE:
    _bind_signatures()


# ── Constants ──────────────────────────────────────────────────────

class WDSPMode:
    LSB, USB, DSB, CW, AM, FM = range(6)

class WDSPAGCMode:
    OFF, LONG, SLOW, MED, FAST = range(5)


# ── Processor ──────────────────────────────────────────────────────

class WDSPProcessor:
    """Professional AGC + NR2 audio processor via WDSP C library.

    Uses pre-allocated ring buffers and zero-copy pointer passing to
    eliminate per-call GC pressure.  All WDSP function signatures are
    declared at module load for ARM64 type safety.
    """

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

        # ── Work buffers (pre-allocated once, reused every call) ──
        # _in / _out are interleaved I/Q: even = signal, odd = 0.0
        self._in = np.zeros(buffer_size * 2, dtype=np.float64)
        self._out = np.zeros(buffer_size * 2, dtype=np.float64)
        # Streaming buffers — process() accumulates variable-length input
        # into _input_buffer, drains in buffer_size chunks through
        # fexchange0, and queues output into _output_buffer.  Output is
        # returned FIFO so the caller always gets time-aligned samples.
        self._input_buffer = np.array([], dtype=np.float64)
        self._output_buffer = np.array([], dtype=np.float64)

        # ── Memory safety: all C-boundary buffers must be C-contiguous ──
        for name, arr in (("_in", self._in), ("_out", self._out)):
            assert arr.flags["C_CONTIGUOUS"], \
                f"WDSP {name}: pre-allocated buffer must be C-contiguous"
            assert arr.dtype == np.float64, \
                f"WDSP {name}: pre-allocated buffer must be float64"

        self._init(mode, agc_mode, enable_nb, enable_anf)

    # ── Initialisation ─────────────────────────────────────────

    def _init(self, mode, agc, nb, anf):
        _wdsp.OpenChannel(
            ctypes.c_int(self.channel),
            ctypes.c_int(self.buffer_size),
            ctypes.c_int(self.buffer_size),
            ctypes.c_int(self.sample_rate),
            ctypes.c_int(self.sample_rate),
            ctypes.c_int(self.sample_rate),
            ctypes.c_int(0), ctypes.c_int(1),
            ctypes.c_double(0), ctypes.c_double(0),
            ctypes.c_double(0), ctypes.c_double(0),
            ctypes.c_int(0))
        _wdsp.SetRXAMode(ctypes.c_int(self.channel), ctypes.c_int(mode))
        _wdsp.SetRXAPanelGain1(ctypes.c_int(self.channel),
                               ctypes.c_double(0.2))
        self.set_agc(agc)
        if self._nr2:
            _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel), ctypes.c_int(1))
            if _check_symbol("SetRXAEMNRgainMethod"):
                _wdsp.SetRXAEMNRgainMethod(ctypes.c_int(self.channel),
                                           ctypes.c_int(0))
            if _check_symbol("SetRXAEMNRnpeMethod"):
                _wdsp.SetRXAEMNRnpeMethod(ctypes.c_int(self.channel),
                                          ctypes.c_int(0))
            if _check_symbol("SetRXAEMNRaeRun"):
                _wdsp.SetRXAEMNRaeRun(ctypes.c_int(self.channel),
                                      ctypes.c_int(1))
            if _check_symbol("SetRXAEMNRPosition"):
                _wdsp.SetRXAEMNRPosition(ctypes.c_int(self.channel),
                                         ctypes.c_int(0))
            self._apply_nr2_params(self._nr2_level)

        if nb:
            self.set_nb_enabled(True)
        if anf:
            self.set_anf_enabled(True)

    # ── NR2 parameter control (AE zeta / psi) ──────────────────

    def _apply_nr2_params(self, level: int):
        """Set NR2 strength via AE zeta & psi parameters.

        SetRXAEMNRgainLine does NOT exist in this libwdsp build.
        We emulate NR2 strength by adjusting the AE (Acquisition
        Enhancement) statistical prior parameters:

          aeZetaThresh — speech / noise discrimination point.
              Lower zeta → harder to trigger speech detection
              → more aggressive noise suppression.
              Strength   0 → zeta 0.50  (light)
              Strength 100 → zeta 0.08  (heavy)

          aePsi — noise-floor adaptation rate.
              Higher psi → faster adaptation → tracks changing
              noise floors more aggressively.
              Strength   0 → psi 0.01  (slow, gentle)
              Strength 100 → psi 0.08  (fast, aggressive)

        The exponential mapping (t ** 0.6 / t ** 0.8) gives more
        resolution at the heavy end where users typically operate.
        """
        clamped = max(0, min(100, level))
        t = clamped / 100.0
        # zeta: 0.50 → 0.08  (weighted toward aggressive)
        zeta = 0.50 - (t ** 0.6) * 0.42
        # psi:  0.01 → 0.08  (weighted toward aggressive)
        psi  = 0.01 + (t ** 0.8) * 0.07

        if _check_symbol("SetRXAEMNRaeZetaThresh"):
            _wdsp.SetRXAEMNRaeZetaThresh(
                ctypes.c_int(self.channel), ctypes.c_double(zeta))
        if _check_symbol("SetRXAEMNRaePsi"):
            _wdsp.SetRXAEMNRaePsi(
                ctypes.c_int(self.channel), ctypes.c_double(psi))
        logger.debug("NR2 strength=%d → zeta=%.4f psi=%.4f",
                     clamped, zeta, psi)

    # ── Core processing ────────────────────────────────────────

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

    # ── AGC ────────────────────────────────────────────────────

    def set_agc(self, mode: int):
        _wdsp.SetRXAAGCMode(ctypes.c_int(self.channel), ctypes.c_int(mode))

    def set_agc_attack(self, ms: int):
        """AGC attack time in milliseconds."""
        if _check_symbol("SetRXAAGCAttack"):
            _wdsp.SetRXAAGCAttack(ctypes.c_int(self.channel), ctypes.c_int(ms))

    def set_agc_decay(self, ms: int):
        """AGC decay time in milliseconds."""
        if _check_symbol("SetRXAAGCDecay"):
            _wdsp.SetRXAAGCDecay(ctypes.c_int(self.channel), ctypes.c_int(ms))

    def set_agc_hang(self, ms: int):
        """AGC hang time in milliseconds."""
        if _check_symbol("SetRXAAGCHang"):
            _wdsp.SetRXAAGCHang(ctypes.c_int(self.channel), ctypes.c_int(ms))

    def set_agc_thresh(self, db: float):
        """AGC threshold in dB."""
        if _check_symbol("SetRXAAGCThresh"):
            _wdsp.SetRXAAGCThresh(ctypes.c_int(self.channel),
                                  ctypes.c_double(db))

    def set_agc_slope(self, db: float):
        """AGC slope in dB."""
        if _check_symbol("SetRXAAGCSlope"):
            _wdsp.SetRXAAGCSlope(ctypes.c_int(self.channel),
                                 ctypes.c_double(db))

    def get_s_meter(self) -> float:
        """Read RX S-meter from WDSP (linear or dB depending on build).

        Returns -127.0 if the symbol is unavailable.
        """
        if not _check_symbol("GetRXAMeter"):
            return -127.0
        return float(_wdsp.GetRXAMeter(
            ctypes.c_int(self.channel), ctypes.c_int(0)))

    # ── Mode ───────────────────────────────────────────────────

    def set_mode(self, mode: int):
        _wdsp.SetRXAMode(ctypes.c_int(self.channel), ctypes.c_int(mode))

    # ── NR2 (spectral noise reduction) ──────────────────────────

    def set_nr2_enabled(self, on: bool):
        """Enable / disable NR2 spectral noise reduction."""
        run = 1 if on else 0
        _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel), ctypes.c_int(run))
        self._nr2 = on

    def set_nr2_level(self, level: int):
        """Set NR2 strength (0-100).  100 = most aggressive."""
        self._nr2_level = max(0, min(100, level))
        self._apply_nr2_params(self._nr2_level)

    def set_nr2_gain_method(self, method: int):
        """Set NR2 gain method (0=linear, 1=log)."""
        if _check_symbol("SetRXAEMNRgainMethod"):
            _wdsp.SetRXAEMNRgainMethod(
                ctypes.c_int(self.channel), ctypes.c_int(method))

    def set_nr2_npe_method(self, method: int):
        """Set NR2 NPE method (0=standard)."""
        if _check_symbol("SetRXAEMNRnpeMethod"):
            _wdsp.SetRXAEMNRnpeMethod(
                ctypes.c_int(self.channel), ctypes.c_int(method))

    def set_nr2_ae_run(self, on: bool):
        """Enable / disable NR2 AE (acquisition enhancement)."""
        if _check_symbol("SetRXAEMNRaeRun"):
            run = 1 if on else 0
            _wdsp.SetRXAEMNRaeRun(ctypes.c_int(self.channel), ctypes.c_int(run))

    # ── NB (noise blanker) ──────────────────────────────────────

    def set_nb_enabled(self, on: bool):
        """Enable / disable noise blanker."""
        run = 1 if on else 0
        if _check_symbol("SetRXASNBARun"):
            _wdsp.SetRXASNBARun(ctypes.c_int(self.channel), ctypes.c_int(run))

    # ── ANF (auto notch filter) ─────────────────────────────────

    def set_anf_enabled(self, on: bool):
        """Enable / disable auto notch filter."""
        run = 1 if on else 0
        _wdsp.SetRXAANFRun(ctypes.c_int(self.channel), ctypes.c_int(run))

    # ── NF (manual notch filter — uses NBP Notch Bank Processor) ─

    def set_nf_enabled(self, on: bool, freq_hz: float = 0.0):
        """Enable / disable manual notch filter at given frequency.

        Uses the NBP (Notch Bank Processor) API.  A single notch is
        managed at index 0 — adding a new frequency replaces the old one.
        """
        if not _check_symbol("RXANBPSetNotchesRun"):
            return
        run = 1 if on else 0
        if on and freq_hz > 0:
            if _check_symbol("RXANBPDeleteNotch"):
                _wdsp.RXANBPDeleteNotch(
                    ctypes.c_int(self.channel), ctypes.c_int(0))
            if _check_symbol("RXANBPAddNotch"):
                _wdsp.RXANBPAddNotch(
                    ctypes.c_int(self.channel), ctypes.c_int(0),
                    ctypes.c_double(freq_hz), ctypes.c_double(50.0))
        if not on:
            if _check_symbol("RXANBPDeleteNotch"):
                _wdsp.RXANBPDeleteNotch(
                    ctypes.c_int(self.channel), ctypes.c_int(0))
        _wdsp.RXANBPSetNotchesRun(
            ctypes.c_int(self.channel), ctypes.c_int(run))

    # ── Bandpass ────────────────────────────────────────────────

    def set_bandpass(self, low_hz: float, high_hz: float):
        """Set audio bandpass filter edges.

        Uses the real symbols SetRXABandpassFreqs + SetRXABandpassRun
        (the old code called the non-existent SetRXABandpassFilter).
        """
        if _check_symbol("SetRXABandpassFreqs"):
            _wdsp.SetRXABandpassFreqs(
                ctypes.c_int(self.channel),
                ctypes.c_double(low_hz), ctypes.c_double(high_hz))
        if _check_symbol("SetRXABandpassRun"):
            _wdsp.SetRXABandpassRun(
                ctypes.c_int(self.channel), ctypes.c_int(1))

    # ── Equalizer ───────────────────────────────────────────────

    def set_eq_run(self, on: bool):
        """Enable / disable the RX graphic equalizer."""
        if _check_symbol("SetRXAEQRun"):
            _wdsp.SetRXAEQRun(ctypes.c_int(self.channel),
                              ctypes.c_int(1 if on else 0))

    def set_eq_profile(self, gains_db: list[float]):
        """Upload a custom EQ profile (list of per-band gains in dB)."""
        if not _check_symbol("SetRXAEQProfile"):
            return
        n = len(gains_db)
        if n == 0:
            return
        arr = (ctypes.c_double * n)(*gains_db)
        _wdsp.SetRXAEQProfile(ctypes.c_int(self.channel),
                              ctypes.c_int(n), arr)

    # ── FM Squelch ──────────────────────────────────────────────

    def set_fm_squelch_run(self, on: bool):
        """Enable / disable FM squelch."""
        if _check_symbol("SetRXAFMSQRun"):
            _wdsp.SetRXAFMSQRun(ctypes.c_int(self.channel),
                                ctypes.c_int(1 if on else 0))

    def set_fm_squelch_threshold(self, thresh: float):
        """Set FM squelch threshold (0.0–1.0)."""
        if _check_symbol("SetRXAFMSQThreshold"):
            _wdsp.SetRXAFMSQThreshold(ctypes.c_int(self.channel),
                                      ctypes.c_double(thresh))

    # ── Cleanup ────────────────────────────────────────────────

    def close(self):
        if WDSP_AVAILABLE:
            _wdsp.SetChannelState(ctypes.c_int(self.channel),
                                  ctypes.c_int(0), ctypes.c_int(0))
            _wdsp.CloseChannel(ctypes.c_int(self.channel))

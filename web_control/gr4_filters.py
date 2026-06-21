"""
GNU Radio 4.0 FIR Filter Design — Ported to Python
=====================================================
Window-method FIR lowpass/bandpass design, ported from:
  gnuradio4/algorithm/include/gnuradio-4.0/algorithm/filter/FilterTool.hpp
  gnuradio4/algorithm/include/gnuradio-4.0/algorithm/fourier/window.hpp

Used by dsp.py for complex SSB bandpass filters with >60 dB sideband rejection.
"""

import numpy as np
from math import pi, sin, cos, sqrt, ceil

# ── Window Functions ──────────────────────────────────────────────

def hamming(n: int) -> np.ndarray:
    i = np.arange(n)
    return (0.54 - 0.46 * np.cos(2 * pi * i / (n - 1))).astype(np.float32)

def hann(n: int) -> np.ndarray:
    i = np.arange(n)
    return (0.5 - 0.5 * np.cos(2 * pi * i / (n - 1))).astype(np.float32)

def blackman(n: int) -> np.ndarray:
    i = np.arange(n)
    return (0.42 - 0.5 * np.cos(2 * pi * i / (n - 1))
            + 0.08 * np.cos(4 * pi * i / (n - 1))).astype(np.float32)

def kaiser(n: int, beta: float = 1.6) -> np.ndarray:
    from scipy.special import i0
    alpha = (n - 1) / 2.0
    w = np.zeros(n)
    for i in range(n):
        arg = beta * sqrt(1.0 - ((i - alpha) / alpha) ** 2)
        w[i] = i0(arg) / i0(beta)
    return w.astype(np.float32)

def create_window(n: int, wtype: str = "hamming", beta: float = 1.6) -> np.ndarray:
    return {"hamming": hamming, "hann": hann,
            "blackman": blackman, "kaiser": lambda: kaiser(n, beta),
            "rectangular": lambda: np.ones(n, dtype=np.float32)
           }.get(wtype.lower(), hamming)(n)


# ── FIR Design ─────────────────────────────────────────────────────

def estimate_kaiser_taps(attenuation_db: float, transition_width: float) -> int:
    n = int(ceil((attenuation_db - 8.0) / (2.285 * transition_width)))
    if n % 2 == 0: n += 1
    return max(n, 7)


def design_fir_lowpass(cutoff: float, sample_rate: float,
                       attenuation_db: float = 60.0,
                       transition_width_hz: float = 500.0,
                       window_type: str = "hamming") -> np.ndarray:
    """Design FIR lowpass using sinc + window method."""
    fc = cutoff / sample_rate
    tw = 2.0 * pi * transition_width_hz / sample_rate
    n_taps = min(estimate_kaiser_taps(attenuation_db, tw), 512)

    beta = 1.6 if window_type == "kaiser" else 0.0
    win = create_window(n_taps, window_type, beta)

    M = (n_taps - 1) / 2.0
    coeffs = np.zeros(n_taps, dtype=np.float64)
    for i in range(n_taps):
        x = i - M
        coeffs[i] = 2.0 * fc if abs(x) < 1e-10 else sin(2.0 * pi * fc * x) / (pi * x)

    coeffs = (coeffs * win).astype(np.float32)
    coeffs /= np.sum(coeffs)
    return coeffs


def design_fir_bandpass(center: float, bandwidth: float, sample_rate: float,
                        window_type: str = "hamming") -> np.ndarray:
    """Design FIR bandpass by frequency-shifting a lowpass prototype."""
    lp = design_fir_lowpass(
        cutoff=bandwidth / 2.0, sample_rate=sample_rate,
        transition_width_hz=bandwidth / 4.0, window_type=window_type)
    n = len(lp); M = (n - 1) / 2.0
    shift = 2.0 * pi * center / sample_rate
    return (lp * 2.0 * np.cos(shift * (np.arange(n) - M))).astype(np.float32)

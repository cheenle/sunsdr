"""
sunmrrc — SunSDR2 DX Mobile Web Control
========================================
"""
import asyncio, json, logging, os, re, struct, subprocess, sys, time, wave
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "web_control"))
import sunsdr_direct
from sunsdr_direct import SunSDR2DXClient
from dsp import (StreamProcessor, SpectrumProcessor, AudioDemodulator,
                 AUDIO_RATE as DSP_AUDIO_RATE,
                 TX_PACKET_INTERVAL_S, TX_SETTLE_PACKETS)
from opus_rx import (RxOpusEncoder, TxOpusDecoder,
                     AUDIO_TAG_PCM, AUDIO_TAG_OPUS, DEFAULT_BITRATE)
import numpy as np

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sunmrrc")

# ── Config ────────────────────────────────────────────────────────
DEVICE_HOST = os.environ.get("DEVICE_HOST", "192.168.16.200")
WEB_PORT = int(os.environ.get("WEB_PORT", "8889"))
STATIC_DIR = Path(__file__).parent / "static"
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "sunmrrc")

# ── Auth ───────────────────────────────────────────────────────────
import hashlib, secrets as _secrets
_auth_tokens: set[str] = set()        # valid session tokens (server-side)
AUTH_COOKIE = "sunmrrc_auth"
AUTH_TOKEN_BYTES = 32

def _make_auth_token() -> str:
    """Generate a new random session token."""
    return _secrets.token_hex(AUTH_TOKEN_BYTES)

def _verify_auth(request: Request) -> bool:
    """Check whether the request carries a valid auth cookie or query-param token.

    The cookie is checked first (fast path for every same-origin request).
    If the cookie is missing OR stale (server restart clears _auth_tokens),
    the query-param token is tried as a fallback.  This matters for
    audioWorklet.addModule() and Worker() constructors, where the frontend
    passes the token via ?token= because some mobile browsers treat worklet
    fetches as anonymous (no cookies).
    """
    token = request.cookies.get(AUTH_COOKIE)
    if token and token in _auth_tokens:
        return True
    token = request.query_params.get("token")
    return token is not None and token in _auth_tokens

# ── App ───────────────────────────────────────────────────────────
radio = SunSDR2DXClient(host=DEVICE_HOST)
dsp_proc: StreamProcessor | None = None
iq_sock = None; tx_counter = 0x04B0; loop_counter = 0
import threading as _threading
tx_counter_lock = _threading.Lock()  # 0xFFFD pacer thread + 0xFFFE keep-alive race on tx_counter


def _next_tx_counter() -> int:
    """Atomically advance the shared TX packet counter (+0x10000) and return it.

    The 0xFFFD pacer thread and the 0xFFFE keep-alive (async loop) both increment
    tx_counter; without this lock a lost +=  yields a non-monotonic / duplicate
    counter, which the device drops → TX buffer underrun → periodic popping.
    """
    global tx_counter
    with tx_counter_lock:
        # Wrap at 2^32: the counter is packed as unsigned 32-bit ('<I') in the
        # 0xFFFD/0xFFFE/keep-alive headers. Without wrapping it overflows after
        # ~7.5h of runtime and struct.pack raises, killing the IQ stream task
        # (→ no spectrum, no audio). The device only uses the low bits.
        tx_counter = (tx_counter + 0x10000) & 0xFFFFFFFF
        return tx_counter
_last_smeter_ts = 0.0  # throttle S-meter broadcasts
_last_telem_ts = 0.0   # throttle TX power/SWR telemetry broadcasts

# TX telemetry (0x1F00, 34 B). Field offsets reverse-engineered from a real
# ExpertSDR3 40m drive sweep (device/captures/expert_40m_drive.pcap, analyzed
# 2026-06-24 against external wattmeter + ExpertSDR3's own power readout):
#   off30 f32 = forward power in WATTS (PEP envelope). Monotonic with drive:
#               28%→3W, 71%→54W, 88%→83W, 100%→101W. Matches ExpertSDR3's own
#               ~95W self-readout at 100%. This is the device's direct float
#               watt reading — NO cubic/linear fit needed.
#   off16 u16 = SUPPLY VOLTAGE × 10 (NOT SWR). Reverse-engineered 2026-06-25:
#               reads ~136 (13.6 V) at idle and DROPS as power rises
#               (0W→13.6, 30-50W→13.1, 80-110W→12.9 V), correlation with
#               forward power = -0.79 — a textbook PSU sag curve, not SWR.
#               The device sends NO reverse-power field (off22 is *average*
#               forward power, ratio to off30 ≈ the 3:1 SSB crest factor), so
#               it cannot compute SWR at all. A stable reading while NOT keyed
#               also rules out SWR. Value is volts = off16 / 10.
#   off18 f32 = PA temperature °C (~42, barely moves with drive).
# The OLD off14 u16 + cubic fit was wrong: off14 is non-monotonic noise
# (28%→72, 45%→51, 100%→81) and never tracked real power. The OLD off16/100
# "SWR" was also wrong — it's the supply voltage (see above).
TELEM_PWR_OFF = 30     # f32 forward power (W)
TELEM_VOLT_OFF = 16    # u16 supply voltage ×10
TELEM_TEMP_OFF = 18    # f32 PA temp °C
_last_tlm_ts = 0.0     # throttle TX power/SWR telemetry broadcasts
_last_pwr_log_ts = 0.0  # throttle TX power logger to 1 Hz
_last_1f01_log_ts = 0.0  # throttle 0x1F01 TX-frame logger to 1 Hz
_seen_sub_ids: set = set()   # diagnostic: unique (sub-id, length) tuples from device

# Connected clients
ctrl_clients: set[WebSocket] = set()
audio_rx_clients: set[WebSocket] = set()
audio_tx_clients: set[WebSocket] = set()
spectrum_clients: set[WebSocket] = set()

# Remote bandwidth control: spectrum is usually the downlink hog. RX Opus at
# 64 kbps is modest, but 512-bin waterfall rows at ~38 fps cost ~155 kbps before
# WebSocket/IP overhead. Keep the local default high and let remote clients drop
# it over /WSCTRX with setSpectrumFps:<fps>.
spectrum_fps = 38.0
_last_spectrum_send_ts = 0.0

# ── Continuous RX-audio → 48 kHz resampler (cubic, cross-chunk) ─────
# Resamples the demodulator's RX audio rate (≈39062.5 Hz for WFM, 15625 Hz for
# voice — fractional and IQ-rate-dependent) up to 48 kHz for Opus + AudioContext.
#
# Uses CUBIC (Catmull-Rom) interpolation, not linear. Linear interp has a sinc²
# passband: upsampling 39062.5→48000, it loses ~4.4 dB at 15 kHz, which dulls
# WFM's high end audibly. Catmull-Rom reads 4 neighbouring input samples per
# output point → much flatter passband (~-1 dB at 15 kHz) and weaker imaging.
#
# Cross-chunk continuity: a fractional read cursor (_rs_phase) plus the last 3
# input samples (_rs_hist) are carried across calls, so reads that straddle a
# chunk boundary interpolate across the seam instead of restarting (the seam
# would otherwise tick on FM, where no AGC masks it). Per-call src_rate lets the
# step change live when the user switches IQ sample rate / voice↔WFM.
RX_OUT_RATE = 48000
_rs_phase = 0.0
_rs_hist = np.zeros(3, dtype=np.float64)  # last 3 prev-chunk samples: pos -3,-2,-1
_rs_step = None      # src_rate / RX_OUT_RATE, recomputed when src_rate changes
_rs_src_rate = None  # last source rate seen, to detect runtime rate changes


def _resample_audio_continuous(arr: np.ndarray, src_rate: float) -> bytes | None:
    """Cubic (Catmull-Rom) resample src_rate → 48 kHz, cross-chunk continuous.

    Output sample k reads input position p_k = _rs_phase + k·step (step =
    src_rate/48000) in THIS chunk's input coordinates (p=0 → arr[0], p=-1 →
    previous chunk's last sample). Catmull-Rom needs the 4 samples idx0-1 …
    idx0+2 around each p; ext[] prepends the 3 carried history samples so the
    low end is reachable, and k is capped so the high end never reads past the
    chunk. Returns Int16 LE bytes @ 48 kHz, or None if the chunk yields nothing.
    """
    global _rs_phase, _rs_hist, _rs_step, _rs_src_rate
    if _rs_step is None or src_rate != _rs_src_rate:
        # Rate changed (or first call) → recompute step. Phase/history are NOT
        # reset here; reset_rx_resampler() does that on PTT / rate change.
        _rs_step = src_rate / RX_OUT_RATE   # input samples per output sample
        _rs_src_rate = src_rate
    n = len(arr)
    if n < 3:
        return None
    af = arr.astype(np.float64)
    # ext index = input_pos + 3: ext[0:3] = history (pos -3,-2,-1),
    # ext[3:] = this chunk (pos 0..n-1). Readable input pos ∈ [-3, n-1].
    ext = np.empty(n + 3, dtype=np.float64)
    ext[0:3] = _rs_hist
    ext[3:] = af
    # Largest k with floor(p_k) ≤ n-3 (so idx0+2 ≤ n-1 stays inside the chunk).
    k_max = int(np.floor((n - 2 - _rs_phase) / _rs_step - 1e-9))
    if k_max < 0:
        # No output this chunk (degenerate; never happens with 512-sample chunks
        # and step<1). Advance phase + history so the next chunk stays seamless.
        _rs_phase -= n
        _rs_hist = af[-3:].copy()
        return None
    k = np.arange(k_max + 1, dtype=np.float64)
    pos = _rs_phase + k * _rs_step                  # input coords
    idx0 = np.floor(pos).astype(np.int64)
    t = pos - idx0
    e = idx0 + 3                                    # ext index of idx0
    a = ext[e - 1]; b = ext[e]; c = ext[e + 1]; d = ext[e + 2]
    t2 = t * t; t3 = t2 * t
    # Catmull-Rom cubic through (b,c) with tangents from a,d.
    out = 0.5 * (2.0 * b
                 + (c - a) * t
                 + (2.0 * a - 5.0 * b + 4.0 * c - d) * t2
                 + (-a + 3.0 * b - 3.0 * c + d) * t3)
    # Next read position, re-expressed in the NEXT chunk's coords (shift by n).
    _rs_phase = (_rs_phase + (k_max + 1) * _rs_step) - n
    _rs_hist = af[-3:].copy()
    return np.clip(out, -32768, 32767).astype('<i2').tobytes()


def reset_rx_resampler():
    """Clear cross-chunk resampler state (PTT toggle, sample-rate change)."""
    global _rs_phase, _rs_hist
    _rs_phase = 0.0
    _rs_hist = np.zeros(3, dtype=np.float64)

# RX audio codec. Opus cuts the ~256 kbit/s Int16 PCM stream to ~18-24 kbit/s.
# Each /WSaudioRX binary frame is prefixed with a 1-byte codec tag (AUDIO_TAG_*)
# so clients decode the right way without a control-channel race. Default ON;
# toggled via the `setOpus:` control command (menu setting). Falls back to PCM
# if libopus is unavailable.
opus_enabled = True
opus_encoder: RxOpusEncoder | None = None
opus_tx_decoder: TxOpusDecoder | None = None
try:
    opus_encoder = RxOpusEncoder(bitrate=DEFAULT_BITRATE)
except Exception as e:
    logger.warning("Opus encoder unavailable, RX stays on Int16 PCM: %s", e)
    opus_enabled = False
try:
    opus_tx_decoder = TxOpusDecoder()
except Exception as e:
    logger.warning("TX Opus decoder unavailable: %s", e)

# Recording state
recording_active = False
recording_buffer: list[bytes] = []  # raw Int16 PCM chunks at the RX audio rate
recording_start_time = 0.0
recording_rate = 0  # RX audio rate (Hz) captured when recording starts; the
                    # demodulator output is ~39062.5 Hz (IQ-rate dependent), NOT
                    # DSP_AUDIO_RATE, so the MP3 must be encoded at this rate or
                    # it plays back at the wrong speed/pitch.
RECORDINGS_DIR = STATIC_DIR / "recordings"
TX_UPLINK_CAPTURE_DIR = Path(__file__).parent / "captures"
TX_WS_JITTER_PRIME_FRAMES = 6       # 120 ms of 20 ms mic frames before drain
TX_WS_JITTER_REPRIME_FRAMES = 3     # 60 ms after a browser/worker stall
TX_WS_JITTER_MAX_FRAMES = 80        # bound latency if a mobile browser bursts


def _write_wav(path: Path, pcm: bytes, rate: int):
    """Write mono Int16 PCM to a WAV file."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(rate))
        wf.writeframes(pcm)


def _save_tx_uplink_capture(raw_pcm: bytes, timed_pcm: bytes,
                            csv_rows: list[str], rate: int) -> tuple[Path, Path, Path]:
    """Save decoded TX mic audio just before the modulator.

    raw WAV concatenates decoded frames exactly as received. timed WAV inserts
    silence for late WebSocket arrivals, making uplink jitter audible.
    """
    TX_UPLINK_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    raw_path = TX_UPLINK_CAPTURE_DIR / f"tx_uplink_pre_mod_{ts}_raw.wav"
    timed_path = TX_UPLINK_CAPTURE_DIR / f"tx_uplink_pre_mod_{ts}_timed.wav"
    csv_path = TX_UPLINK_CAPTURE_DIR / f"tx_uplink_pre_mod_{ts}.csv"
    latest_raw = TX_UPLINK_CAPTURE_DIR / "tx_uplink_pre_mod_latest_raw.wav"
    latest_timed = TX_UPLINK_CAPTURE_DIR / "tx_uplink_pre_mod_latest_timed.wav"
    latest_csv = TX_UPLINK_CAPTURE_DIR / "tx_uplink_pre_mod_latest.csv"

    _write_wav(raw_path, raw_pcm, rate)
    _write_wav(timed_path, timed_pcm, rate)
    _write_wav(latest_raw, raw_pcm, rate)
    _write_wav(latest_timed, timed_pcm, rate)
    csv_text = "".join(csv_rows)
    csv_path.write_text(csv_text, encoding="utf-8")
    latest_csv.write_text(csv_text, encoding="utf-8")
    logger.info("TX uplink capture saved: raw=%s timed=%s csv=%s",
                raw_path, timed_path, csv_path)
    return raw_path, timed_path, csv_path

MIME = {".css":"text/css", ".js":"application/javascript", ".html":"text/html",
        ".json":"application/json", ".png":"image/png", ".wav":"audio/wav",
        ".mp3":"audio/mpeg", ".wasm":"application/wasm"}

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0a0a0f">
<title>SunMRRC — Login</title>
<link rel="stylesheet" href="/mobile.css">
<style>
  body {{ display:flex; align-items:center; justify-content:center; min-height:100vh;
         background:var(--bg-primary); font-family:-apple-system,BlinkMacSystemFont,sans-serif; }}
  .login-card {{ background:var(--bg-card); border:1px solid var(--border-color);
                 border-radius:16px; padding:32px 24px; width:100%; max-width:360px;
                 text-align:center; }}
  .login-card h1 {{ color:var(--accent-primary); font-size:24px; margin:0 0 8px; }}
  .login-card p {{ color:var(--text-secondary); font-size:14px; margin:0 0 24px; }}
  .login-card input {{ width:100%; padding:12px; border-radius:10px;
                       border:1px solid var(--border-color); background:var(--bg-secondary);
                       color:var(--text-primary); font-size:16px; text-align:center;
                       box-sizing:border-box; -webkit-appearance:none; }}
  .login-card input:focus {{ outline:none; border-color:var(--accent-primary); }}
  .login-card button {{ width:100%; margin-top:16px; padding:12px; border:none;
                        border-radius:10px; background:var(--accent-primary);
                        color:#000; font-size:16px; font-weight:600; cursor:pointer; }}
  .login-card button:active {{ opacity:0.8; }}
  .login-card .error {{ color:var(--accent-danger); font-size:13px; margin-top:12px; display:none; }}
</style>
</head>
<body>
<div class="login-card">
  <h1>&#x269B; SunMRRC</h1>
  <p>Enter password to access radio</p>
  <input type="password" id="pwd" placeholder="Password" autofocus autocomplete="current-password">
  <button id="btn">Sign In</button>
  <div class="error" id="err">Wrong password</div>
</div>
<script>
var next = "{next}";
var btn = document.getElementById('btn');
var inp = document.getElementById('pwd');
var err = document.getElementById('err');
async function tryLogin() {{
  var pwd = inp.value;
  if (!pwd) return;
  btn.disabled = true; err.style.display = 'none';
  try {{
    var r = await fetch('/api/auth/login', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{password:pwd, next:next}})
    }});
    if (r.ok) {{
      var data = await r.json();
      window.location.href = data.next || '/';
    }} else {{
      err.style.display = 'block'; btn.disabled = false; inp.focus();
    }}
  }} catch(e) {{ err.style.display = 'block'; btn.disabled = false; }}
}}
btn.addEventListener('click', tryLogin);
inp.addEventListener('keydown', function(e) {{ if (e.key==='Enter') tryLogin(); }});
</script>
</body>
</html>"""


# ── Lifespan ──────────────────────────────────────────────────────
def _load_tune_wav(path: Path) -> np.ndarray:
    """Load a mono WAV file and resample to DSP audio rate (15625 Hz).

    Returns float32 array normalized to [-1, 1], or empty array on failure.
    """
    try:
        with wave.open(str(path), 'rb') as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            nf = wf.getnframes()
            raw = wf.readframes(nf)
        if sw == 2:
            pcm = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
        else:
            logger.warning(f"Tune WAV: unsupported sample width {sw}")
            return np.array([], dtype=np.float32)
        if nch > 1:
            pcm = pcm.reshape(-1, nch).mean(axis=1)  # downmix to mono
        # Resample to DSP audio rate if needed
        if sr != DSP_AUDIO_RATE:
            out_len = int(len(pcm) * DSP_AUDIO_RATE / sr)
            pcm = np.interp(np.linspace(0, len(pcm)-1, out_len),
                            np.arange(len(pcm)), pcm).astype(np.float32)
        logger.info(f"Tune WAV loaded: {len(pcm)} samples @ {DSP_AUDIO_RATE} Hz ({len(pcm)/DSP_AUDIO_RATE:.1f}s)")
        return pcm
    except Exception as e:
        logger.warning(f"Tune WAV load failed: {e}")
        return np.array([], dtype=np.float32)


def _save_recording() -> str:
    """Encode the recording buffer to MP3 via ffmpeg. Returns the filename."""
    global recording_buffer, recording_start_time, recording_rate
    if not recording_buffer:
        return ""
    # Concatenate all Int16 PCM chunks
    raw = b''.join(recording_buffer)
    recording_buffer = []
    if len(raw) < 640:  # need at least ~20ms of audio
        return ""
    # Recording is the demodulator's RAW RX audio (≈39062.5 Hz, captured before
    # the 48 kHz resample), so encode at the rate it was actually captured at —
    # using DSP_AUDIO_RATE (the TX chain's 15625 Hz) would slow/detune playback.
    rec_rate = int(round(recording_rate)) if recording_rate else int(DSP_AUDIO_RATE)
    duration = len(raw) / (2 * rec_rate)  # 2 bytes per sample
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(recording_start_time))
    filename = f"MRRC_{ts}_{duration:.0f}s.mp3"
    filepath = RECORDINGS_DIR / filename

    # Pipe raw Int16 PCM to ffmpeg → MP3
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "s16le", "-ar", str(rec_rate), "-ac", "1",
             "-i", "pipe:0",
             "-c:a", "libmp3lame", "-b:a", "64k",
             "-f", "mp3", "pipe:1"],
            input=raw, capture_output=True, timeout=30)
        if proc.returncode == 0 and proc.stdout:
            filepath.write_bytes(proc.stdout)
            out_size = len(proc.stdout)
            logger.info(f"Recording saved: {filename} ({duration:.1f}s, {out_size} bytes MP3)")
            return filename
        else:
            logger.error(f"ffmpeg MP3 encode failed: {proc.stderr.decode()[:200]}")
            return ""
    except FileNotFoundError:
        logger.error("ffmpeg not found — cannot encode MP3")
        return ""
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg MP3 encode timed out")
        return ""
    except Exception as e:
        logger.error(f"MP3 encode error: {e}")
        return ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dsp_proc
    # Ensure recordings directory exists
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    ok = await radio.connect()
    logger.info(f"SunSDR2DX: {ok}")
    # Load persisted per-band TX power and push it into sunsdr_direct so
    # set_frequency() applies the user's configured power on every QSY.
    try:
        _apply_band_power(_load_band_power())
    except Exception as e:
        logger.warning(f"band_power load failed: {e}")
    dsp_proc = StreamProcessor(
        spectrum=SpectrumProcessor(fft_size=2048),
        demodulator=AudioDemodulator())
    # Pre-load tune.wav into modulator for tune-mode playback
    tune_path = STATIC_DIR / "tune.wav"
    if tune_path.is_file():
        tune_data = _load_tune_wav(tune_path)
        if len(tune_data) > 0 and dsp_proc.modulator:
            dsp_proc.modulator.set_tune_wav(tune_data)
    asyncio.create_task(_heartbeat_task())
    asyncio.create_task(_process_iq_stream())
    yield
    # Shutdown
    if dsp_proc and dsp_proc.demodulator._wdsp:
        try:
            dsp_proc.demodulator._wdsp.close()
        except Exception:
            pass

app = FastAPI(title="SunMRRC", lifespan=lifespan)

# ── Auth middleware ─────────────────────────────────────────────────
# Protects every HTTP route except /login, /api/auth/*, and static
# assets.  WebSocket endpoints do their own token check in on-accept.
# JS / WASM / CSS / images are intentionally public — they contain zero
# secrets (the repo is open-source) and blocking them with 401 breaks
# audioWorklet.addModule() / Worker() fetches on browsers that treat
# those as anonymous (no-cookie).  The real security boundary is the
# WebSocket upgrade + /api/* handlers, each of which verifies the token
# independently.
_PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/auth/logout"}
_PUBLIC_PATH_PREFIXES = ("/mobile.css",)
_STATIC_EXT_PUBLIC = (".js", ".wasm", ".json", ".css", ".html", ".svg",
                      ".png", ".ico", ".wav", ".mp3", ".ttf", ".woff2")

@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    # Allow public paths without auth
    if path in _PUBLIC_PATHS:
        return await call_next(request)
    # Allow static assets (CSS, JS, WASM, images, fonts, etc.) without
    # auth — they're needed by the login page itself and by audio
    # worklet / worker fetches that may not send cookies.
    if path.startswith(_PUBLIC_PATH_PREFIXES):
        return await call_next(request)
    if path.endswith(_STATIC_EXT_PUBLIC) or path.startswith("/modules/"):
        return await call_next(request)
    if not _verify_auth(request):
        # API routes get a 401 JSON response
        if path.startswith("/api/") or path.startswith("/WS"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Page routes (/, /index.html, etc.) redirect to login
        qs = request.url.query
        redirect_url = f"/login?next={path}"
        if qs:
            redirect_url += f"&{qs}"
        return RedirectResponse(url=redirect_url, status_code=302)
    return await call_next(request)


# ── Auth routes ─────────────────────────────────────────────────────
@app.get("/login")
async def _login_page(request: Request):
    """Serve the login page."""
    next_url = request.query_params.get("next", "/")
    # Prevent open redirect: only allow same-origin paths
    if next_url.startswith("//") or next_url.startswith("http:") or next_url.startswith("https:"):
        next_url = "/"
    return HTMLResponse(LOGIN_HTML.format(next=next_url))


@app.post("/api/auth/login")
async def _api_login(request: Request):
    """Validate password and issue a session token."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid request"}, status_code=400)
    pwd = body.get("password", "")
    if pwd != WEB_PASSWORD:
        logger.warning("Auth: failed login attempt from %s", request.client.host if request.client else "?")
        return JSONResponse({"error": "wrong password"}, status_code=401)
    token = _make_auth_token()
    _auth_tokens.add(token)
    logger.info("Auth: new session token issued (%d active)", len(_auth_tokens))
    next_url = body.get("next", "/")
    if next_url.startswith("//") or next_url.startswith("http:") or next_url.startswith("https:"):
        next_url = "/"
    resp = JSONResponse({"ok": True, "next": next_url})
    resp.set_cookie(AUTH_COOKIE, token, httponly=False, samesite="strict", max_age=86400*30)
    return resp


@app.post("/api/auth/logout")
async def _api_logout(request: Request):
    """Invalidate the current session token."""
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        _auth_tokens.discard(token)
    return JSONResponse({"ok": True})


@app.get("/api/auth/check")
async def _api_auth_check(request: Request):
    """Check whether the current request is authenticated (used by login page JS)."""
    if _verify_auth(request):
        return JSONResponse({"authenticated": True})
    return JSONResponse({"authenticated": False})


# ── Memory channels persistence ───────────────────────────────────
MEM_CHANNELS_FILE = Path(__file__).parent / "mem_channels.json"

def _load_mem_channels():
    try:
        if MEM_CHANNELS_FILE.is_file():
            data = json.loads(MEM_CHANNELS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("channels"), list):
                return data
    except Exception:
        pass
    return {"channels": [None] * 6}

def _save_mem_channels(data):
    try:
        MEM_CHANNELS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── API routes (must be before static catch-all) ──────────────────
@app.get("/api/mem_channels")
async def api_get_mem_channels():
    return _load_mem_channels()


@app.post("/api/mem_channels")
async def api_post_mem_channels(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    channels = payload.get("channels", [])
    padded = []
    for ch in (channels if isinstance(channels, list) else []):
        if isinstance(ch, dict):
            padded.append({
                "freq": ch.get("freq", 0),
                "mode": ch.get("mode", "USB"),
                "label": str(ch.get("label", ""))[:32],
            })
        else:
            padded.append(None)
    while len(padded) < 6:
        padded.append(None)
    data = {"channels": padded[:6]}
    _save_mem_channels(data)
    return data


# ── Per-band TX power persistence ─────────────────────────────────
# Runtime-editable per-band drive %. Persisted to band_power.json and pushed
# into sunsdr_direct via set_band_power() so set_frequency() applies the right
# power on every QSY. The frontend reads/writes it via /api/band_power.
BAND_POWER_FILE = Path(__file__).parent / "band_power.json"

def _default_band_power():
    """Build the default config dict from sunsdr_direct's built-in table."""
    return {
        "bands": [
            {"low": lo, "high": hi, "power": pct}
            for (lo, hi, pct) in sunsdr_direct.BAND_POWER
        ],
        "default": sunsdr_direct.BAND_POWER_DEFAULT,
    }

def _load_band_power():
    try:
        if BAND_POWER_FILE.is_file():
            data = json.loads(BAND_POWER_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("bands"), list):
                return data
    except Exception:
        pass
    return _default_band_power()

def _save_band_power(data):
    try:
        BAND_POWER_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _apply_band_power(data):
    """Push a band-power config dict into sunsdr_direct's runtime table."""
    try:
        table = [(int(b["low"]), int(b["high"]), int(b["power"]))
                 for b in data.get("bands", []) if isinstance(b, dict)]
        default = data.get("default", sunsdr_direct.BAND_POWER_DEFAULT)
        sunsdr_direct.set_band_power(table, default)
    except Exception as e:
        logger.warning(f"apply band_power failed: {e}")


@app.get("/api/band_power")
async def api_get_band_power():
    return _load_band_power()


@app.post("/api/band_power")
async def api_post_band_power(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    bands = []
    for b in (payload.get("bands", []) if isinstance(payload, dict) else []):
        if isinstance(b, dict) and "low" in b and "high" in b and "power" in b:
            try:
                bands.append({
                    "low": int(b["low"]),
                    "high": int(b["high"]),
                    "power": max(0, min(100, int(b["power"]))),
                })
            except (ValueError, TypeError):
                continue
    default = sunsdr_direct.BAND_POWER_DEFAULT
    try:
        default = max(0, min(100, int(payload.get("default", default))))
    except (ValueError, TypeError):
        pass
    data = {"bands": bands, "default": default}
    _save_band_power(data)
    _apply_band_power(data)
    # Re-apply to the current frequency immediately so the change takes effect
    # without waiting for the next QSY.
    try:
        if radio.rx_freq:
            radio.drive = sunsdr_direct.band_power_for(radio.rx_freq)
            await radio._send_drive_byte()
    except Exception:
        pass
    return data


@app.get("/api/status")
async def api_status():
    """Health-check endpoint."""
    return {
        "connected": radio.connected if radio else False,
        "dsp": dsp_proc is not None,
        "clients": {
            "ctrl": len(ctrl_clients),
            "audio_rx": len(audio_rx_clients),
            "audio_tx": len(audio_tx_clients),
            "spectrum": len(spectrum_clients),
        }
    }


# ── Recordings API ─────────────────────────────────────────────────
@app.get("/api/recordings")
async def api_list_recordings():
    """List saved recordings, newest first."""
    files = []
    if RECORDINGS_DIR.is_dir():
        for f in sorted(RECORDINGS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            sfx = f.suffix.lower()
            if sfx in ('.mp3', '.wav'):
                info = {
                    "filename": f.name,
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                }
                # Parse duration from filename "MRRC_YYYYMMDD_HHMMSS_Ns.mp3"
                m = re.search(r'_(\d+)s\.\w+$', f.name)
                if m:
                    info["duration_s"] = float(m.group(1))
                elif sfx == '.wav' and f.stat().st_size > 44:
                    info["duration_s"] = round(f.stat().st_size / (2 * DSP_AUDIO_RATE), 1)
                files.append(info)
    return {"recordings": files}


@app.get("/api/recordings/{filename}")
async def api_download_recording(filename: str):
    """Download a specific recording."""
    # Sanitize filename to prevent path traversal
    safe = Path(filename).name
    fp = RECORDINGS_DIR / safe
    if not fp.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = fp.suffix.lower()
    mime = "audio/mpeg" if ext == ".mp3" else "audio/wav"
    return Response(
        content=fp.read_bytes(),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{safe}"'}
    )


@app.delete("/api/recordings/{filename}")
async def api_delete_recording(filename: str):
    """Delete a recording."""
    safe = Path(filename).name
    fp = RECORDINGS_DIR / safe
    if fp.is_file():
        fp.unlink()
        return {"deleted": safe}
    return JSONResponse({"error": "not found"}, status_code=404)


# ── Static file serving (catch-all, must be LAST route) ───────────
@app.get("/{p:path}")
async def serve_static(p: str):
    fp = STATIC_DIR / (p if p else "index.html")
    if fp.is_file():
        ext = Path(p).suffix if p else ".html"
        return Response(fp.read_bytes(), media_type=MIME.get(ext, "text/html"))
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


# ── IQ Processing ─────────────────────────────────────────────────
async def _heartbeat_task():
    """Send 0x0018 heartbeat every 0.5s to keep device session alive."""
    import socket as sm
    hb_sock = sm.socket(sm.AF_INET, sm.SOCK_DGRAM)
    try:
        hb_sock.bind(("192.168.16.100", 50001))
    except OSError:
        hb_sock.close()
        hb_sock = sm.socket(sm.AF_INET, sm.SOCK_DGRAM)
        hb_sock.setsockopt(sm.SOL_SOCKET, sm.SO_REUSEADDR, 1)
        # Fallback: send from any available port
    hdr = struct.pack("<HHIIH", 0xFF32, 0x0018, 4, 0x00010000, 0) + struct.pack("<I", 0)
    while radio.connected:
        try:
            hb_sock.sendto(hdr, ("192.168.16.200", 50001))
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def _process_iq_stream():
    global iq_sock, tx_counter, loop_counter, _last_smeter_ts, _last_telem_ts
    import socket as sm

    iq_sock = sm.socket(sm.AF_INET, sm.SOCK_DGRAM)
    iq_sock.setblocking(False)
    iq_sock.setsockopt(sm.SOL_SOCKET, sm.SO_REUSEADDR, 1)
    iq_sock.bind(("192.168.16.100", 50002))
    logger.info("IQ: port 50002")

    loop = asyncio.get_event_loop()
    pkt_count = 0; iq_count = 0; spec_count = 0; audio_count = 0
    last_stats = time.monotonic()
    last_keepalive = time.monotonic()
    # Verified from device/captures/sunsdr_sdr_tx.pcap: ExpertSDR3 paces TX IQ
    # at 5.12 ms/packet (39063 Hz, RX/2), NOT 2.56 ms. See PROTOCOL.md §17.
    TX_INTERVAL = TX_PACKET_INTERVAL_S  # 0.00512 s per packet (195.3 Hz)
    # TX state shared with _tx_pacer_thread
    tx_thread = None
    tx_thread_stop = False
    tx_keepalive_due = False

    def _build_one_tx_packet(silent: bool = False) -> bytes:
        """Build a single 1210-byte TX IQ packet (header + payload).

        silent=True emits a zero-IQ packet (used for the PA/relay settling
        pad after PTT assert; see PROTOCOL.md §17.5).
        """
        ctr = _next_tx_counter()
        hdr = struct.pack("<HHIH", 0xFF32, 0xFFFD, ctr, 0x0102)
        if silent:
            return hdr + b'\x00' * 1200
        tx_data = dsp_proc.get_tx_iq() if dsp_proc else None
        if tx_data is None:
            tx_data = b'\x00' * 1200
        if len(tx_data) < 1200:
            tx_data = tx_data + b'\x00' * (1200 - len(tx_data))
        elif len(tx_data) > 1200:
            tx_data = tx_data[:1200]
        return hdr + tx_data

    def _tx_pacer_thread():
        """Dedicated OS thread — time.sleep()-paced TX at TX_PACKET_INTERVAL_S
        (5.12ms, 195 pkt/s, 39063 Hz) to match ExpertSDR3. See PROTOCOL.md §17.

        ADAPTIVE PACING: the browser produces mic frames at a rate slightly
        below the fixed pacer consumption (measured ≈43 vs ≈49 pkt/s on the
        test session, a ~5 pkt/s deficit). A fixed cadence would drain a 120 ms
        buffer in ~17 s, causing periodic underflow/stutter. So the pacer
        tracks an EMA of queue depth and scales the per-packet interval
        within ±15%: when the queue is below target it slows down, letting
        production refill it; when above target it speeds back up to nominal.
        A deeper 1024-pkt buffer absorbs the short-term jitter (see dsp.py).
        """
        global tx_keepalive_due
        # Adaptive pacing state
        _Q_TARGET = 70.0                     # aim to hover around ~360 ms queued
        _ADAPTIVE_ALPHA = 0.08               # EMA over ~12 packets (~60 ms)
        _ADAPTIVE_MAX = 0.15                 # never deviate more than ±15% from nominal
        _q_ema = _Q_TARGET                   # start at target to avoid transient
        # Diagnostic probe: every 20th packet (~10 Hz) write a CSV row with
        # actual send interval, queue depth, silence flag, and whether the
        # pacer fell behind. Keeps overhead low while capturing real per-
        # packet timing from the pacer thread itself.
        _PROBE_PATH = "/tmp/tx_probe.csv"
        _PROBE_EVERY = 20
        _probe_cnt = 0
        _probe_last = time.monotonic()
        try:
            # "w" (truncate) NOT "a" (append): each TX pacer start gets a fresh
            # file. The old append mode accumulated days of history across many
            # parameter versions + a header row per session, so any whole-file
            # analysis mixed stale data with current — a real debugging trap.
            _probe_file = open(_PROBE_PATH, "w", buffering=1)  # line-buffered
        except Exception:
            _probe_file = None
        if _probe_file and _probe_cnt == 0:
            _probe_file.write("mono_ts,interval_ms,q_depth,silent,behind_ms\n")
        # TX IQ power probe accumulators (see inline comment below).
        _pwr_sum_sq = 0.0
        _pwr_peak = 0.0
        _pwr_n = 0
        _pwr_n_pkts = 0
        _pwr_log_last = time.monotonic()
        next_pkt = time.monotonic()
        # PA/relay settling pad: zero-IQ packets before real modulation.
        settle_left = TX_SETTLE_PACKETS
        while not tx_thread_stop and radio.connected:
            silent_flag = False
            try:
                if settle_left > 0:
                    iq_sock.sendto(_build_one_tx_packet(silent=True),
                                   (DEVICE_HOST, 50002))
                    settle_left -= 1
                    silent_flag = True
                else:
                    pkt = _build_one_tx_packet()
                    iq_sock.sendto(pkt, (DEVICE_HOST, 50002))
                    silent_flag = (pkt[10:12] == b'\x00\x00' and pkt[12:14] == b'\x00\x00')
                    # ── TX IQ power probe: decode first 20 IQ samples of
                    # every non-silent packet, accumulate peak/RMS, log 1 Hz.
                    # The firmware has no ALC, so IQ amplitude we send is
                    # what the PA actually transmits. Once the user provides
                    # a known power reading (e.g. from a wattmeter during
                    # tune), a linear calibration turns IQ magnitude → watts.
                    if not silent_flag and len(pkt) >= 130:
                        _pwr_n_pkts += 1
                        payload = pkt[10:]
                        for _i in range(20):
                            _o = _i * 6
                            _iv = int.from_bytes(payload[_o:_o+3], 'little', signed=True)
                            _qv = int.from_bytes(payload[_o+3:_o+6], 'little', signed=True)
                            _mag = ((_iv * _iv + _qv * _qv) ** 0.5) / 8388608.0
                            _pwr_sum_sq += _mag * _mag
                            _pwr_n += 1
                            if _mag > _pwr_peak:
                                _pwr_peak = _mag
            except Exception:
                pass
            # Adaptive interval: EMA of queue depth, scale within ±15%.
            try:
                if dsp_proc and dsp_proc.modulator is not None:
                    with dsp_proc.modulator._mic_lock:
                        _q_now = float(len(dsp_proc.modulator._mic_iq))
                else:
                    _q_now = _Q_TARGET
            except Exception:
                _q_now = _Q_TARGET
            _q_ema = _ADAPTIVE_ALPHA * _q_now + (1 - _ADAPTIVE_ALPHA) * _q_ema
            err = (_q_ema - _Q_TARGET) / _Q_TARGET          # -1..+1 range
            scale = 1.0 - max(-_ADAPTIVE_MAX, min(_ADAPTIVE_MAX, err))
            adaptive_iv = TX_INTERVAL * scale
            next_pkt += adaptive_iv
            now = time.monotonic()
            delay = next_pkt - now
            if delay > 0:
                time.sleep(delay)
            else:
                # Fell behind – reset clock
                if delay < -0.005:
                    tx_keepalive_due = True  # signal RX loop: we're behind, send keep-alive soon
                next_pkt = now + adaptive_iv
            # Probe (every Nth packet): record timing, queue depth, flags
            _probe_cnt += 1
            if _probe_file and (_probe_cnt % _PROBE_EVERY == 0):
                interval_ms = (now - _probe_last) * 1000.0
                behind_ms = max(0.0, -(next_pkt - now - adaptive_iv) * 1000.0)
                q_depth = int(_q_now)
                try:
                    _probe_file.write(
                        f"{now:.6f},{interval_ms:.3f},{q_depth},"
                        f"{1 if silent_flag else 0},{behind_ms:.3f}\n")
                except Exception:
                    pass
                _probe_last = now
            # 1 Hz: flush the TX IQ power accumulator to server.log.
            if _pwr_n > 0 and (now - _pwr_log_last) >= 1.0:
                _rms = (_pwr_sum_sq / _pwr_n) ** 0.5
                # Underflow count is a jitter-buffer health metric: each
                # underrun inserts a silence packet (amplitude step to 0 →
                # click/dropout the far end hears). Previously incremented
                # but never logged — a blind spot. Surface it here at 1 Hz.
                try:
                    _ur = dsp_proc.modulator._mic_underruns if (
                        dsp_proc and dsp_proc.modulator) else -1
                except Exception:
                    _ur = -1
                logger.info(
                    f"TX IQ pwr: peak={_pwr_peak:.4f}  rms={_rms:.4f}  "
                    f"n={_pwr_n}  pkts={_pwr_n_pkts}  underruns={_ur}")
                _pwr_sum_sq = 0.0
                _pwr_peak = 0.0
                _pwr_n = 0
                _pwr_n_pkts = 0
                _pwr_log_last = now
        if _probe_file:
            try:
                _probe_file.close()
            except Exception:
                pass

    import threading as th

    while radio.connected:
        if getattr(radio, '_ptt_active', False):
            # ── Start dedicated TX pacer thread ──────────────────
            # Re-arm the amplitude ramp so the first packets of real IQ fade
            # in 0→1, removing the hard step out of the zero-IQ settling pad.
            if dsp_proc and dsp_proc.modulator:
                dsp_proc.modulator.reset_tx_ramp()
                dsp_proc.modulator.reset_mic()  # drop any stale pre-PTT mic queue
            tx_thread_stop = False
            tx_thread = th.Thread(target=_tx_pacer_thread, daemon=True)
            tx_thread.start()
            # Wait for PTT release — keep receiving telemetry during TX
            while getattr(radio, '_ptt_active', False) and radio.connected:
                now = time.monotonic()
                if tx_keepalive_due or (now - last_keepalive >= 0.5):
                    ctr = _next_tx_counter()
                    ka_hdr = struct.pack("<HHIH", 0xFF32, 0xFFFE, ctr, 0x0001)
                    try:
                        iq_sock.sendto(ka_hdr + b'\x00' * 1200, (DEVICE_HOST, 50002))
                    except Exception:
                        pass
                    last_keepalive = now
                    tx_keepalive_due = False
                # Non-blocking receive: process telemetry (0x1F00) during TX
                try:
                    data = await asyncio.wait_for(loop.sock_recvfrom(iq_sock, 65536), timeout=0.05)
                    raw_rx = data[0]
                    if len(raw_rx) >= 10 and raw_rx[0] == 0x32 and raw_rx[1] == 0xff:
                        sub = struct.unpack('<H', raw_rx[2:4])[0]
                        if sub == 0x1F00 and len(raw_rx) >= 34:
                            if now - _last_telem_ts >= 0.1:
                                _last_telem_ts = now
                                try:
                                    watts = struct.unpack_from('<f', raw_rx, TELEM_PWR_OFF)[0]
                                    volt_raw = struct.unpack_from('<H', raw_rx, TELEM_VOLT_OFF)[0]
                                    volts = volt_raw / 10.0  # off16 u16 / 10 = supply V
                                    temp_c = struct.unpack_from('<f', raw_rx, TELEM_TEMP_OFF)[0]
                                    asyncio.ensure_future(_send_ctrl(
                                        f"getTXTelem:{watts:.1f},{volts:.1f},{temp_c:.0f},{int(watts)}"))
                                except Exception:
                                    pass
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    pass
            # PTT released – stop TX thread
            tx_thread_stop = True
            if tx_thread and tx_thread.is_alive():
                tx_thread.join(timeout=1.0)
            tx_thread = None
            loop_counter += 1
            continue

        # ── Send keep-alive / stream request even when idle ──────
        # The device needs periodic 0xFFFE packets to maintain the IQ stream.
        # Without this, the stream may stall and never start after boot.
        now = time.monotonic()
        if now - last_keepalive >= 0.5:
            ctr = _next_tx_counter()
            hdr = struct.pack("<HHIH", 0xFF32, 0xFFFE, ctr, 0x0001)
            try: iq_sock.sendto(hdr + b'\x00'*1200, ("192.168.16.200", 50002))
            except: pass
            last_keepalive = now

        try:
            data = await asyncio.wait_for(loop.sock_recvfrom(iq_sock, 65536), timeout=0.5)
            raw = data[0]; loop_counter += 1; pkt_count += 1
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Time-based stats even when no IQ data arrives
            now = time.monotonic()
            if now - last_stats >= 5.0:
                logger.info(
                    f"IQ idle {now - last_stats:.0f}s: pkt=0 iq=0 "
                    f"connected={radio.connected} "
                    f"ctrl={len(ctrl_clients)} rx={len(audio_rx_clients)} "
                    f"spec_ws={len(spectrum_clients)}")
                last_stats = now
            continue
        except Exception: continue

        if len(raw) < 10 or raw[0] != 0x32 or raw[1] != 0xff:
            continue
        sub = struct.unpack('<H', raw[2:4])[0]

        # ── One-off sub-ID census: log every unique (sub, length) combo once
        # so we can verify what telemetry sub-IDs the device actually sends.
        # Remove after the diagnostic session.
        global _seen_sub_ids
        key = (sub, len(raw))
        if key not in _seen_sub_ids:
            _seen_sub_ids.add(key)
            logger.info(f"DEVICE SUB-ID: 0x{sub:04X} len={len(raw)} "
                        f"(raw[:16]={raw[:16].hex()})")

        if sub == 0xFFFE and len(raw) >= 1200:
            iq_count += 1
            payload = raw[10:]; n = min(200, len(payload)//6)
            iq = np.zeros(n, dtype=np.complex64)
            for i in range(n):
                off = i*6
                if off+6 > len(payload): break
                iv = int.from_bytes(payload[off:off+3], 'little', signed=True)
                qv = int.from_bytes(payload[off+3:off+6], 'little', signed=True)
                iq[i] = complex(iv/8388608.0, qv/8388608.0)
            dsp_proc.feed_iq(iq)

            if dsp_proc.latest_spectrum is not None:
                spec_count += 1
                spec = dsp_proc.latest_spectrum; dsp_proc.latest_spectrum = None
                # Throttle S-meter broadcasts to ~5 Hz (every 200ms)
                global _last_smeter_ts
                now_spec = time.monotonic()
                if now_spec - _last_smeter_ts >= 0.2:
                    _last_smeter_ts = now_spec
                    p90 = float(np.percentile(spec, 90))
                    s9 = max(0, min(60, int(9 + (p90 + 73)/6)))
                    asyncio.ensure_future(_send_ctrl(f"getSignalLevel:{s9}"))
                if spectrum_clients:
                    asyncio.ensure_future(_broadcast_spectrum(spec))

            audio = dsp_proc.get_audio()
            if audio:
                audio_count += 1
                asyncio.ensure_future(_broadcast_audio(audio))

        elif sub == 0x1F00 and len(raw) >= 34:
            # Device→PC TX telemetry (0x1F00, 34 B). Power = off30 f32 (watts),
            # supply voltage = off16 u16 / 10, temp = off18 f32. See TELEM_*_OFF
            # comment above for the reverse-engineering provenance (real 40m
            # drive sweep vs external wattmeter). off30 is the device's direct
            # float watt reading — no fit. off16 is PSU voltage, NOT SWR (the
            # device sends no reverse-power field). Needs len>=34 (off30 f32
            # spans bytes 30-33).
            now_t = time.monotonic()
            if now_t - _last_telem_ts >= 0.1:  # throttle to ~10 Hz
                _last_telem_ts = now_t
                try:
                    watts = struct.unpack_from('<f', raw, TELEM_PWR_OFF)[0]
                    volts = struct.unpack_from('<H', raw, TELEM_VOLT_OFF)[0] / 10.0
                    temp_c = struct.unpack_from('<f', raw, TELEM_TEMP_OFF)[0]
                    asyncio.ensure_future(_send_ctrl(
                        f"getTXTelem:{watts:.1f},{volts:.1f},{temp_c:.0f},{watts:.0f}"))
                    global _last_pwr_log_ts
                    if (getattr(radio, '_ptt_active', False)
                            and now_t - _last_pwr_log_ts >= 1.0):
                        _last_pwr_log_ts = now_t
                        logger.info(
                            f"[TX] 0x1F00 W={watts:.1f} "
                            f"V={volts:.1f} T={temp_c:.0f}C")
                except Exception as e:
                    logger.warning(f"0x1F00 parse error: {e}")

        elif sub == 0x1F01 and len(raw) >= 22:
            # TX-only frame marker (trailing bit 16 toggles during TX).
            # Forward power is in 0x1F00 off30 f32 (watts); this is a flag only.
            pass

        # Periodic stats (doesn't send keep-alive — that's handled above)
        now = time.monotonic()
        if now - last_stats >= 5.0:
            logger.info(
                f"IQ stats: pkt={pkt_count} iq={iq_count} "
                f"spec={spec_count} audio={audio_count} "
                f"connected={radio.connected} "
                f"ctrl={len(ctrl_clients)} rx={len(audio_rx_clients)} "
                f"spec_ws={len(spectrum_clients)}")
            last_stats = now; pkt_count = 0; iq_count = 0
            spec_count = 0; audio_count = 0


async def _send_ctrl(msg: str):
    dead = set()
    for ws in list(ctrl_clients):
        try: await ws.send_text(msg)
        except: dead.add(ws)
    ctrl_clients.difference_update(dead)


async def _broadcast_audio(pcm: bytes):
    # Capture raw audio for recording (at native DSP_AUDIO_RATE, before resampling)
    global recording_active, recording_buffer
    if recording_active:
        recording_buffer.append(pcm)

    if not audio_rx_clients: return
    arr = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
    if len(arr) < 16: return
    # Source rate is the demodulator's actual RX audio rate (≈39062.5 Hz, varies
    # with IQ sample rate), NOT DSP_AUDIO_RATE (the TX chain's 15625 Hz).
    src_rate = dsp_proc.demodulator.audio_rate if dsp_proc else DSP_AUDIO_RATE
    pcm16 = _resample_audio_continuous(arr, float(src_rate))
    if pcm16 is None: return

    # Build the list of tagged WS frames to send. Each frame is a 1-byte codec
    # tag + payload so the client decodes correctly without a control-channel
    # race. Opus emits one frame per complete 960-sample (20 ms) packet; the
    # encoder buffers partial tails internally to keep frame boundaries clean.
    frames: list[bytes] = []
    if opus_enabled and opus_encoder is not None:
        try:
            for pkt in opus_encoder.push(pcm16):
                frames.append(bytes([AUDIO_TAG_OPUS]) + pkt)
        except Exception as e:
            logger.warning("Opus encode failed, sending PCM: %s", e)
            frames = [bytes([AUDIO_TAG_PCM]) + pcm16]
    else:
        frames = [bytes([AUDIO_TAG_PCM]) + pcm16]

    if not frames: return
    dead = set()
    for ws in list(audio_rx_clients):
        try:
            for frame in frames:
                await ws.send_bytes(frame)
        except: dead.add(ws)
    audio_rx_clients.difference_update(dead)


async def _broadcast_spectrum(spec):
    """Push one spectrum frame to waterfall clients as a compact uint8 array.

    spec: list of ~512 dB values clipped to [-120, 0]. We quantize each bin to
    a single byte (0 = -120 dB, 255 = 0 dB) — 512 bytes/frame, ~19 KB/s @ 38 Hz.
    The browser maps bytes back to a colour ramp for the waterfall row.
    """
    global _last_spectrum_send_ts
    if not spectrum_clients: return
    now = time.monotonic()
    min_interval = 1.0 / max(1.0, float(spectrum_fps))
    if now - _last_spectrum_send_ts < min_interval:
        return
    _last_spectrum_send_ts = now
    arr = np.asarray(spec, dtype=np.float32)
    if arr.size == 0: return
    q = np.clip((arr + 120.0) * (255.0 / 120.0), 0, 255).astype(np.uint8)
    frame = q.tobytes()
    dead = set()
    for ws in list(spectrum_clients):
        try: await ws.send_bytes(frame)
        except: dead.add(ws)
    spectrum_clients.difference_update(dead)


# ── WebSocket: Spectrum (/WSspectrum) ─────────────────────────────
@app.websocket("/WSspectrum")
async def ws_spectrum(ws: WebSocket):
    token = ws.query_params.get("token", "")
    if token not in _auth_tokens:
        await ws.accept(); await ws.close(code=4001, reason="auth required"); return
    await ws.accept(); spectrum_clients.add(ws)
    try:
        while True: await ws.receive()
    except (WebSocketDisconnect, RuntimeError): pass
    finally: spectrum_clients.discard(ws)


# ── WebSocket: Control (/WSCTRX) ──────────────────────────────────
@app.websocket("/WSCTRX")
async def ws_ctrl(ws: WebSocket):
    global recording_active, recording_buffer, recording_start_time, recording_rate
    token = ws.query_params.get("token", "")
    if token not in _auth_tokens:
        await ws.accept(); await ws.close(code=4001, reason="auth required"); return
    await ws.accept(); ctrl_clients.add(ws)
    try:
        while True:
            msg = await ws.receive_text()
            # 心跳:前端发无冒号的 "PING",立即回 "PONG"。
            # 必须在下面的冒号检查之前处理,否则 PING 会被丢弃,
            # 导致前端 showlatency() 永不触发(状态栏时延一直 --ms)。
            if msg == "PING":
                await ws.send_text("PONG")
                continue
            if ':' not in msg: continue
            cmd, _, val = msg.partition(':')
            try:
                if cmd == "getFreq":
                    await ws.send_text(f"getFreq:{int(radio.rx_freq or 14074000)}")
                elif cmd == "getMode":
                    # SDR: hardware only digitizes; mode lives in the DSP demodulator
                    dsp_mode = dsp_proc.demodulator.mode if dsp_proc else "USB"
                    await ws.send_text(f"getMode:{dsp_mode}")
                elif cmd == "getPTT":
                    await ws.send_text(f"getPTT:{str(getattr(radio,'ptt',False)).lower()}")
                elif cmd == "setFreq":
                    await radio.set_frequency(float(val))
                    await ws.send_text(f"getFreq:{val}")
                elif cmd == "setMode":
                    # SDR: mode is purely a software DSP concept.
                    # Hardware gets no mode command — the IQ stream is mode-agnostic.
                    mode = val.upper()
                    if dsp_proc:
                        dsp_proc.demodulator.set_mode(mode)
                        if dsp_proc.modulator:
                            dsp_proc.modulator.set_mode(mode)  # TX uses same mode
                    await ws.send_text(f"getMode:{mode}")
                elif cmd == "setSampleRate":
                    # Spectrum/IQ width selector (39k/78k/156k/312k).
                    # Reboots device with 0x0001 word[11] = rate index.
                    ok = await radio.set_sample_rate(val.lower())
                    if ok:
                        # Update DSP pipeline to match new IQ rate
                        new_hz = sunsdr_direct.SAMPLE_RATES.get(val.lower(), 78125)
                        if dsp_proc:
                            dsp_proc.set_iq_sample_rate(new_hz)
                        await _send_ctrl(f"setSampleRate:{val.lower()}")
                elif cmd == "setOpus":
                    # RX audio codec toggle. "on"/"true" → Opus (~18-24 kbps),
                    # else Int16 PCM (~256 kbps). Each /WSaudioRX frame carries a
                    # 1-byte codec tag so the client always knows how to decode.
                    global opus_enabled
                    want = val.lower() in ("on", "true", "1", "opus")
                    if want and opus_encoder is None:
                        # libopus unavailable at startup — stay on PCM.
                        await _send_ctrl("setOpus:unavailable")
                    else:
                        opus_enabled = want
                        if opus_encoder is not None:
                            opus_encoder.reset()
                        logger.info("RX codec: %s", "Opus" if want else "Int16 PCM")
                        await _send_ctrl(f"setOpus:{'on' if want else 'off'}")
                elif cmd == "getOpus":
                    if opus_encoder is None:
                        await ws.send_text("setOpus:unavailable")
                    else:
                        await ws.send_text(f"setOpus:{'on' if opus_enabled else 'off'}")
                elif cmd == "setOpusBitrate":
                    # RX Opus bitrate in kbps (e.g. 32/64/96/128). Lets the user
                    # trade bandwidth vs quality per use-case: WFM music sounds
                    # great at 64k while using 1/12 the bandwidth of 768k PCM, so
                    # it streams smoothly on a remote link where PCM stutters.
                    # Older frontend builds accidentally sent bps (32000) instead
                    # of kbps (32); normalize that instead of clamping to 128k.
                    if opus_encoder is not None:
                        try:
                            raw_kbps = int(float(val))
                            if raw_kbps > 1000:
                                raw_kbps = round(raw_kbps / 1000)
                            kbps = max(16, min(128, raw_kbps))
                            opus_encoder.set_bitrate(kbps * 1000)
                            opus_encoder.reset()
                            logger.info("RX Opus bitrate: %d kbps", kbps)
                            await _send_ctrl(f"setOpusBitrate:{kbps}")
                        except ValueError:
                            pass
                elif cmd == "getOpusBitrate":
                    if opus_encoder is not None:
                        await ws.send_text(
                            f"setOpusBitrate:{opus_encoder.bitrate // 1000}")
                elif cmd == "setSpectrumFps":
                    global spectrum_fps
                    try:
                        fps = max(1.0, min(38.0, float(val)))
                        spectrum_fps = fps
                        logger.info("Spectrum FPS cap: %.1f", fps)
                        await _send_ctrl(f"setSpectrumFps:{fps:g}")
                    except ValueError:
                        pass
                elif cmd == "getSpectrumFps":
                    await ws.send_text(f"setSpectrumFps:{spectrum_fps:g}")
                elif cmd == "setPTT":
                    tx = val.lower() == "true"
                    await radio.set_ptt(tx)
                    if dsp_proc: dsp_proc.demodulator.set_ptt(tx)
                    logger.info(f"PTT {'ON ' if tx else 'OFF'}")
                    await ws.send_text(f"getPTT:{str(tx).lower()}")
                elif cmd == "tune":
                    tune_on = val.lower() == "true"
                    await radio.set_tune(tune_on)
                    if dsp_proc and dsp_proc.modulator:
                        dsp_proc.modulator.activate_tune(tune_on)
                elif cmd == "setDrive":
                    # Hardware TX power: the slider (0-100) now drives the DEVICE
                    # via 0x0017 (set_drive sends the sqrt-taper byte). This is the
                    # real PA power control — the device has no ALC, so this is how
                    # ExpertSDR3's Drive slider works too. The software modulator
                    # make-up gain stays fixed; overdriving it was the old broken
                    # workaround that caused the "broken/splattery" audio.
                    pct = max(0, min(100, int(float(val))))
                    await radio.set_drive(pct)
                    await _send_ctrl(f"setDrive:{pct}")
                elif cmd == "setAFGain":
                    vol = float(val) / 100.0
                    if dsp_proc: dsp_proc.demodulator.set_volume(vol)
                    await radio.set_volume(vol)
                elif cmd == "setRFGain":
                    await radio.set_rf_gain(float(val)/100.0)
                elif cmd == "setATT":
                    # Hardware ATT/preamp: 0=-20dB, 1=-10dB, 2=0dB, 3=+10dB
                    level = max(0, min(3, int(float(val))))
                    await radio.set_attenuator(level)
                    await _send_ctrl(f"setATT:{level}")
                elif cmd == "setPreamp":
                    await radio.set_preamp(val.lower() == "true")
                elif cmd == "setAGC":
                    await radio.set_agc_mode(val.upper() if val else "AUTO")
                elif cmd == "setFilter":
                    parts = val.split(',')
                    if len(parts) >= 2:
                        lo, hi = int(parts[0]), int(parts[1])
                        if dsp_proc: dsp_proc.demodulator.reconfigure_filter(low_hz=lo, high_hz=hi)
                        await radio.set_filter(lo, hi)
                # ── WDSP DSP chain (RX noise reduction) ──────────────
                # All WDSP processing is software-side in the demodulator;
                # the radio hardware is never involved. Each handler updates
                # the demodulator and broadcasts the change to every client
                # so multiple devices stay in sync.
                elif cmd == "setWDSPEnabled":
                    on = val.lower() in ("true", "enabled", "1")
                    if dsp_proc: dsp_proc.demodulator.set_wdsp_enabled(on)
                    await _send_ctrl(f"setWDSPEnabled:{'enabled' if on else 'disabled'}")
                elif cmd == "setWDSPNR2Level":
                    lvl = int(val) if val else 0
                    if dsp_proc: dsp_proc.demodulator.set_nr2_level(lvl)
                    await _send_ctrl(f"setWDSPNR2Level:{lvl}")
                elif cmd == "setWDSPNR2":
                    on = val.lower() == "true"
                    if dsp_proc: dsp_proc.demodulator.set_nr2_enabled(on)
                    await _send_ctrl(f"setWDSPNR2:{val.lower()}")
                elif cmd == "setWDSPNB":
                    on = val.lower() == "true"
                    if dsp_proc: dsp_proc.demodulator.set_nb_enabled(on)
                    await _send_ctrl(f"setWDSPNB:{val.lower()}")
                elif cmd == "setWDSPANF":
                    on = val.lower() == "true"
                    if dsp_proc: dsp_proc.demodulator.set_anf_enabled(on)
                    await _send_ctrl(f"setWDSPANF:{val.lower()}")
                elif cmd == "setWDSPNFEnabled":
                    on = val.lower() == "true"
                    if dsp_proc: dsp_proc.demodulator.set_nf_enabled(on)
                    await _send_ctrl(f"setWDSPNFEnabled:{val.lower()}")
                elif cmd == "setWDSPNR2GainMethod":
                    method = int(val) if val else 0
                    if dsp_proc: dsp_proc.demodulator.set_nr2_gain_method(method)
                    await _send_ctrl(f"setWDSPNR2GainMethod:{method}")
                elif cmd == "setWDSPNR2NpeMethod":
                    method = int(val) if val else 0
                    if dsp_proc: dsp_proc.demodulator.set_nr2_npe_method(method)
                    await _send_ctrl(f"setWDSPNR2NpeMethod:{method}")
                elif cmd == "setWDSPNR2AeRun":
                    on = val.lower() == "true"
                    if dsp_proc: dsp_proc.demodulator.set_nr2_ae_run(on)
                    await _send_ctrl(f"setWDSPNR2AeRun:{val.lower()}")
                elif cmd == "setWDSPBandpass":
                    parts = val.split(',')
                    if len(parts) >= 2:
                        lo, hi = float(parts[0]), float(parts[1])
                        if dsp_proc: dsp_proc.demodulator.set_bandpass(lo, hi)
                        await _send_ctrl(f"setWDSPBandpass:{lo},{hi}")
                elif cmd == "setWDSPAGC":
                    mode = int(val) if val else 0
                    if dsp_proc: dsp_proc.demodulator.set_agc_mode(mode)
                    await _send_ctrl(f"setWDSPAGC:{mode}")
                elif cmd == "addWDSPNotch":
                    parts = val.split(',')
                    if dsp_proc and len(parts) >= 2:
                        fc, fw = float(parts[0]), float(parts[1])
                        idx = dsp_proc.demodulator.add_notch(fc, fw)
                        await _send_ctrl(f"addWDSPNotch:{idx},{fc},{fw}")
                elif cmd == "editWDSPNotch":
                    parts = val.split(',')
                    if dsp_proc and len(parts) >= 3:
                        i, fc, fw = int(parts[0]), float(parts[1]), float(parts[2])
                        dsp_proc.demodulator.edit_notch(i, fc, fw)
                        await _send_ctrl(f"editWDSPNotch:{i},{fc},{fw}")
                elif cmd == "deleteWDSPNotch":
                    if dsp_proc and val:
                        dsp_proc.demodulator.delete_notch(int(val))
                        await _send_ctrl(f"deleteWDSPNotch:{val}")
                elif cmd in ("getWDSPStatus", "getWDSPNotches"):
                    status = dsp_proc.demodulator.get_wdsp_status() if dsp_proc else {}
                    await ws.send_text(f"wdspStatus:{json.dumps(status)}")
                # ── Recording (server-side RX audio capture) ─────────
                elif cmd == "startRecording":
                    if not recording_active:
                        recording_active = True
                        recording_buffer = []
                        recording_start_time = time.time()
                        # Capture the demodulator's actual RX audio rate now so
                        # the MP3 is encoded at the rate it was recorded at
                        # (~39062.5 Hz, IQ-rate dependent) — not the TX 15625 Hz.
                        recording_rate = (dsp_proc.demodulator.audio_rate
                                          if dsp_proc else DSP_AUDIO_RATE)
                        logger.info("Recording started (%.1f Hz)", recording_rate)
                    await _send_ctrl("recordingStatus:started")
                elif cmd == "stopRecording":
                    if recording_active:
                        recording_active = False
                        filename = _save_recording()
                        if filename:
                            logger.info(f"Recording stopped: {filename}")
                            await _send_ctrl(f"recordingSaved:{filename}")
                        else:
                            logger.info("Recording stopped: no audio captured")
                    await _send_ctrl("recordingStatus:stopped")
                # ── Safety / misc ────────────────────────────────────
                elif cmd == "s":
                    # TX-audio-channel backup PTT release: force RX.
                    await radio.set_ptt(False)
                    if dsp_proc: dsp_proc.demodulator.set_ptt(False)
                    await _send_ctrl("getPTT:false")
                elif cmd == "cq":
                    # SunMRRC has no server-side CQ voice playback (TX audio
                    # modulation is unsolved), so just acknowledge completion
                    # to keep the client state machine unstuck.
                    await _send_ctrl("cq:complete")
            except Exception as e:
                logger.error(f"CTRL {cmd}: {e}")
    except (WebSocketDisconnect, RuntimeError): pass
    except Exception: pass
    finally: ctrl_clients.discard(ws)


# ── WebSocket: ATR-1000 Tuner (/WSATR1000) ─────────────────────
@app.websocket("/WSATR1000")
async def ws_atr1000(ws: WebSocket):
    """ATR-1000 antenna tuner proxy — bidirectional JSON relay."""
    token = ws.query_params.get("token", "")
    if token not in _auth_tokens:
        await ws.accept(); await ws.close(code=4001, reason="auth required"); return
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
                action = data.get("action", "")
                if action == "sync":
                    # Heartbeat — don't log to avoid flooding
                    pass
                elif action in ("start", "stop"):
                    logger.info(f"ATR1000: {action}")
                else:
                    logger.debug(f"ATR1000: {data}")
            except json.JSONDecodeError:
                pass
    except (WebSocketDisconnect, RuntimeError):
        pass


# ── WebSocket: Audio RX/TX ────────────────────────────────────────
@app.websocket("/WSaudioRX")
async def ws_audio_rx(ws: WebSocket):
    token = ws.query_params.get("token", "")
    if token not in _auth_tokens:
        await ws.accept(); await ws.close(code=4001, reason="auth required"); return
    await ws.accept(); audio_rx_clients.add(ws)
    try:
        while True: await ws.receive()
    except (WebSocketDisconnect, RuntimeError): pass
    finally: audio_rx_clients.discard(ws)


@app.websocket("/WSaudioTX")
async def ws_audio_tx(ws: WebSocket):
    """Mic uplink. Binary frames are Int16 PCM (16 kHz mono, 320 samples/frame
    from controls.js); text frames are control: 'm:rate,encode,...' settings,
    's:' stop. PCM is fed to the TX modulator, which queues 0xFFFD IQ packets
    for the TX pacer thread to drain. See PROTOCOL.md §17 and CLAUDE.md AD-009."""
    token = ws.query_params.get("token", "")
    if token not in _auth_tokens:
        await ws.accept(); await ws.close(code=4001, reason="auth required"); return
    await ws.accept(); audio_tx_clients.add(ws)
    tx_rate = 16000  # mic sample rate from controls.js (48k downsampled ×3)
    loop = asyncio.get_running_loop()
    _lvl_log_last = time.monotonic()  # 1 Hz throttle for the TX chain level probe
    # Probe: record browser mic frame arrival timestamps + sizes to see
    # whether the WS stream itself is jittery (WiFi/bursty) or steady.
    _RX_PROBE = "/tmp/tx_rx_probe.csv"
    try:
        _rx_probe = open(_RX_PROBE, "a", buffering=1)
        _rx_probe.write("mono_ts,interval_ms,bytes,ptt\n")
    except Exception:
        _rx_probe = None
    _rx_last = time.monotonic()
    _cap_active = False
    _cap_raw = bytearray()
    _cap_timed = bytearray()
    _cap_rows = ["mono_ts,interval_ms,tag,wire_bytes,pcm_samples,frame_ms,gap_ms,gap_samples,ptt\n"]
    _cap_last_ts = None
    _cap_rate = tx_rate
    _cap_max_bytes = 16000 * 2 * 180  # 3 minutes at 16 kHz mono Int16
    _tx_pcm_queue: list[tuple[bytes, int, int]] = []
    _tx_queue_lock = asyncio.Lock()
    _tx_pacer_stop = asyncio.Event()
    _tx_pacer_task: asyncio.Task | None = None
    _tx_primed = False
    _tx_first_prime = True
    _tx_last_pace_ts = time.monotonic()

    def _finish_tx_capture(reason: str):
        nonlocal _cap_active, _cap_raw, _cap_timed, _cap_rows, _cap_last_ts, _cap_rate
        if not _cap_active or not _cap_raw:
            _cap_active = False
            _cap_raw = bytearray()
            _cap_timed = bytearray()
            _cap_rows = ["mono_ts,interval_ms,tag,wire_bytes,pcm_samples,frame_ms,gap_ms,gap_samples,ptt\n"]
            _cap_last_ts = None
            return
        try:
            _cap_rows.append(f"# finish,{reason},raw_bytes,{len(_cap_raw)},timed_bytes,{len(_cap_timed)}\n")
            _save_tx_uplink_capture(bytes(_cap_raw), bytes(_cap_timed), _cap_rows, _cap_rate)
        except Exception as e:
            logger.warning("TX uplink capture save failed: %s", e)
        _cap_active = False
        _cap_raw = bytearray()
        _cap_timed = bytearray()
        _cap_rows = ["mono_ts,interval_ms,tag,wire_bytes,pcm_samples,frame_ms,gap_ms,gap_samples,ptt\n"]
        _cap_last_ts = None

    def _capture_tx_pcm(pcm: bytes, tag: int, wire_bytes: int,
                        now_ts: float, interval_ms: float):
        nonlocal _cap_active, _cap_raw, _cap_timed, _cap_rows, _cap_last_ts, _cap_rate
        ptt = 1 if getattr(radio, '_ptt_active', False) else 0
        if not ptt:
            _finish_tx_capture("ptt-off")
            return
        if not _cap_active:
            _cap_active = True
            _cap_rate = tx_rate
            _cap_last_ts = None
            logger.info("TX uplink pre-mod capture started")
        samples = len(pcm) // 2
        frame_ms = samples * 1000.0 / max(1, _cap_rate)
        gap_ms = 0.0
        gap_samples = 0
        if _cap_last_ts is not None:
            observed_ms = (now_ts - _cap_last_ts) * 1000.0
            late_ms = observed_ms - frame_ms
            if late_ms > 30.0:
                gap_ms = late_ms
                gap_samples = int(round(gap_ms * _cap_rate / 1000.0))
                _cap_timed.extend(b"\x00" * (gap_samples * 2))
        _cap_raw.extend(pcm)
        _cap_timed.extend(pcm)
        _cap_rows.append(
            f"{now_ts:.6f},{interval_ms:.3f},{tag},{wire_bytes},"
            f"{samples},{frame_ms:.3f},{gap_ms:.3f},{gap_samples},{ptt}\n")
        _cap_last_ts = now_ts
        if len(_cap_raw) >= _cap_max_bytes:
            _finish_tx_capture("max-duration")

    async def _feed_tx_pcm(pcm: bytes, tag: int, wire_bytes: int,
                           _pace_now: float, _pace_interval_ms: float):
        nonlocal _lvl_log_last
        _capture_tx_pcm(pcm, tag, wire_bytes, _pace_now, _pace_interval_ms)
        if dsp_proc and dsp_proc.modulator and getattr(radio, '_ptt_active', False):
            try:
                await loop.run_in_executor(
                    None, dsp_proc.modulator.feed_audio, pcm, tx_rate)
            except Exception:
                pass
            # ── End-to-end TX chain level probe (1 Hz) ──
            try:
                _now_lvl = time.monotonic()
                if _now_lvl - _lvl_log_last >= 1.0:
                    _lvl_log_last = _now_lvl
                    _s = dsp_proc.modulator.snapshot_levels()
                    def _rms(sq, n): return (sq/n)**0.5 if n else 0.0
                    logger.info(
                        f"TX chain in : rms={_rms(_s['in_sq'],_s['in_n']):.4f} "
                        f"peak={_s['in_pk']:.4f} n={_s['in_n']}")
                    logger.info(
                        f"TX chain an : rms={_rms(_s['an_sq'],_s['an_n']):.4f} "
                        f"peak={_s['an_pk']:.4f} n={_s['an_n']}")
                    logger.info(
                        f"TX chain drv: rms={_rms(_s['drv_sq'],_s['drv_n']):.4f} "
                        f"peak={_s['drv_pk']:.4f} n={_s['drv_n']}")
                    logger.info(
                        f"TX chain lim: rms={_rms(_s['lim_sq'],_s['lim_n']):.4f} "
                        f"peak={_s['lim_pk']:.4f} n={_s['lim_n']}")
            except Exception:
                pass

    async def _tx_uplink_pacer():
        nonlocal _tx_primed, _tx_first_prime, _tx_last_pace_ts
        while not _tx_pacer_stop.is_set():
            item = None
            async with _tx_queue_lock:
                if not getattr(radio, '_ptt_active', False):
                    _tx_pcm_queue.clear()
                    _tx_primed = False
                    _tx_first_prime = True
                else:
                    want = (TX_WS_JITTER_PRIME_FRAMES if _tx_first_prime
                            else TX_WS_JITTER_REPRIME_FRAMES)
                    if not _tx_primed and len(_tx_pcm_queue) >= want:
                        _tx_primed = True
                        _tx_first_prime = False
                    if _tx_primed and _tx_pcm_queue:
                        item = _tx_pcm_queue.pop(0)
                    elif _tx_primed:
                        _tx_primed = False
            if item is None:
                _tx_last_pace_ts = time.monotonic()
                _tx_frame_s = 0.020
                await asyncio.sleep(_tx_frame_s)
                continue
            pcm, tag, wire_bytes = item
            _pace_now = time.monotonic()
            _pace_interval_ms = (_pace_now - _tx_last_pace_ts) * 1000.0
            _tx_last_pace_ts = _pace_now
            await _feed_tx_pcm(pcm, tag, wire_bytes, _pace_now, _pace_interval_ms)
            samples = len(pcm) // 2
            _tx_frame_s = samples / max(1, tx_rate)
            await asyncio.sleep(_tx_frame_s)

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            _rx_now = time.monotonic()
            data = msg.get("bytes")
            if data is not None:
                _interval_ms = (_rx_now - _rx_last) * 1000.0
                if _rx_probe:
                    ptt = 1 if getattr(radio, '_ptt_active', False) else 0
                    try:
                        _rx_probe.write(
                            f"{_rx_now:.6f},{_interval_ms:.3f},"
                            f"{len(data)},{ptt}\n")
                    except Exception:
                        pass
                _rx_last = _rx_now
                # Decode codec-tagged mic frame → PCM → modulator.
                # Tag byte: 0x00 = Int16 PCM, 0x01 = Opus (like RX path).
                tag = -1
                if len(data) >= 2:
                    tag = data[0]
                    frame = data[1:]
                    if tag == AUDIO_TAG_OPUS and opus_tx_decoder:
                        pcm = opus_tx_decoder.decode(frame)
                        if not pcm:
                            continue
                    elif tag == AUDIO_TAG_PCM:
                        pcm = frame
                    else:
                        # legacy: untagged or unknown tag, assume PCM
                        if tag not in (0x00, 0x01):
                            logger.warning("TX audio: unknown tag 0x%02X, len=%d", tag, len(data))
                        pcm = data
                else:
                    pcm = data
                async with _tx_queue_lock:
                    _tx_pcm_queue.append((pcm, tag, len(data)))
                    while len(_tx_pcm_queue) > TX_WS_JITTER_MAX_FRAMES:
                        _tx_pcm_queue.pop(0)
                if _tx_pacer_task is None or _tx_pacer_task.done():
                    _tx_pacer_stop.clear()
                    _tx_pacer_task = asyncio.create_task(_tx_uplink_pacer())
                continue
            text = msg.get("text")
            if text:
                if text.startswith("m:"):
                    # settings: rate,encode,opusRate,opusFrameDur
                    try:
                        parts = text[2:].split(",")
                        tx_rate = int(float(parts[0]))
                    except Exception:
                        pass
                elif text.startswith("s:"):
                    async with _tx_queue_lock:
                        _tx_pcm_queue.clear()
                        _tx_primed = False
                        _tx_first_prime = True
                    _finish_tx_capture("stop-text")
                    if dsp_proc and dsp_proc.modulator:
                        dsp_proc.modulator.reset_mic()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        _tx_pacer_stop.set()
        if _tx_pacer_task is not None:
            _tx_pacer_task.cancel()
            try:
                await _tx_pacer_task
            except (asyncio.CancelledError, RuntimeError):
                pass
        _finish_tx_capture("disconnect")
        audio_tx_clients.discard(ws)


# ── Main ──────────────────────────────────────────────────────────
def _find_ssl():
    """Locate TLS cert/key. Returns (certfile, keyfile) or (None, None).

    iOS Safari only treats HTTPS origins as secure contexts: getUserMedia
    (MIC) and reliable AudioContext autoplay-unlock require it. Set
    DISABLE_SSL=1 to force plain HTTP (e.g. localhost dev).
    """
    if os.environ.get("DISABLE_SSL") == "1":
        return None, None
    certs = Path(__file__).parent / "certs"
    cert = certs / "fullchain.pem"
    key = certs / "radio.vlsc.net.key"
    if cert.is_file() and key.is_file():
        return str(cert), str(key)
    return None, None


if __name__ == "__main__":
    ssl_cert, ssl_key = _find_ssl()
    scheme = "https" if ssl_cert else "http"
    pwd_display = "***" if os.environ.get("WEB_PASSWORD") else "(default: sunmrrc)"
    logger.info(f"sunmrrc {scheme}://[::]:{WEB_PORT}  password={pwd_display}")
    if ssl_cert:
        logger.info(f"TLS: {ssl_cert}")
        uvicorn.run(app, host="::", port=WEB_PORT, reload=False,
                    log_level="info", ssl_certfile=ssl_cert, ssl_keyfile=ssl_key)
    else:
        logger.warning("TLS 证书未找到,以 HTTP 启动 (iOS 无声音/无麦克风)")
        uvicorn.run(app, host="::", port=WEB_PORT, reload=False, log_level="info")

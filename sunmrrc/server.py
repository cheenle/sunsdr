"""
sunmrrc — SunSDR2 DX Mobile Web Control
========================================
"""
import asyncio, json, logging, os, re, struct, subprocess, sys, time, wave
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "web_control"))
from sunsdr_direct import SunSDR2DXClient
from dsp import (StreamProcessor, SpectrumProcessor, AudioDemodulator,
                 AUDIO_RATE as DSP_AUDIO_RATE,
                 TX_PACKET_INTERVAL_S, TX_SETTLE_PACKETS)
import numpy as np

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, HTMLResponse, JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sunmrrc")

# ── Config ────────────────────────────────────────────────────────
DEVICE_HOST = os.environ.get("DEVICE_HOST", "192.168.16.200")
WEB_PORT = int(os.environ.get("WEB_PORT", "8889"))
STATIC_DIR = Path(__file__).parent / "static"

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
        tx_counter += 0x10000
        return tx_counter
_last_smeter_ts = 0.0  # throttle S-meter broadcasts
_last_telem_ts = 0.0   # throttle TX power/SWR telemetry broadcasts

# TX telemetry calibration (0x1F00 off14 u16 → watts). Verified idle baseline
# ~9, ~49 into a dummy load (~5 W per TXPLAN). Linear approximation; calibrate
# TELEM_PWR_SCALE against a known TX power for accuracy.
TELEM_PWR_BASELINE = 9.0
TELEM_PWR_SCALE = 0.125   # (49-9)*0.125 ≈ 5.0 W
_last_tlm_ts = 0.0     # throttle TX power/SWR telemetry broadcasts

# Connected clients
ctrl_clients: set[WebSocket] = set()
audio_rx_clients: set[WebSocket] = set()
audio_tx_clients: set[WebSocket] = set()
spectrum_clients: set[WebSocket] = set()

# Recording state
recording_active = False
recording_buffer: list[bytes] = []  # raw Int16 PCM chunks at DSP_AUDIO_RATE
recording_start_time = 0.0
RECORDINGS_DIR = STATIC_DIR / "recordings"

MIME = {".css":"text/css", ".js":"application/javascript", ".html":"text/html",
        ".json":"application/json", ".png":"image/png", ".wav":"audio/wav",
        ".mp3":"audio/mpeg", ".wasm":"application/wasm"}


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
    global recording_buffer, recording_start_time
    if not recording_buffer:
        return ""
    # Concatenate all Int16 PCM chunks
    raw = b''.join(recording_buffer)
    recording_buffer = []
    if len(raw) < 640:  # need at least ~20ms of audio
        return ""
    duration = len(raw) / (2 * DSP_AUDIO_RATE)  # 2 bytes per sample
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(recording_start_time))
    filename = f"MRRC_{ts}_{duration:.0f}s.mp3"
    filepath = RECORDINGS_DIR / filename

    # Pipe raw Int16 PCM to ffmpeg → MP3
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "s16le", "-ar", str(DSP_AUDIO_RATE), "-ac", "1",
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
        (5.12ms, 195 pkt/s, 39063 Hz) to match ExpertSDR3. See PROTOCOL.md §17."""
        global tx_keepalive_due
        next_pkt = time.monotonic()
        # PA/relay settling pad: zero-IQ packets before real modulation.
        settle_left = TX_SETTLE_PACKETS
        while not tx_thread_stop and radio.connected:
            try:
                if settle_left > 0:
                    iq_sock.sendto(_build_one_tx_packet(silent=True),
                                   (DEVICE_HOST, 50002))
                    settle_left -= 1
                else:
                    iq_sock.sendto(_build_one_tx_packet(), (DEVICE_HOST, 50002))
            except Exception:
                pass
            # Precise interval via wall-clock tracking
            next_pkt += TX_INTERVAL
            now = time.monotonic()
            delay = next_pkt - now
            if delay > 0:
                time.sleep(delay)
            else:
                # Fell behind – reset clock
                if delay < -0.005:
                    tx_keepalive_due = True  # signal RX loop: we're behind, send keep-alive soon
                next_pkt = now + TX_INTERVAL

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
            # Wait for PTT release
            while getattr(radio, '_ptt_active', False) and radio.connected:
                # Send 0xFFFE keep-alive during TX
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
                await asyncio.sleep(0.1)
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

        elif sub == 0x1F00 and len(raw) >= 30:
            # Device→PC TX telemetry (verified from sunsdr_sdr_tx.pcap; see
            # PROTOCOL.md §17.6 and device/data/tx_analysis.json):
            #   off14 u16  = forward-power reading (idle ~9, TX into dummy ~49)
            #   off18 f32  = PA temperature °C (~45)
            #   off26 f32  = SWR (1.0 = matched)
            now_t = time.monotonic()
            if now_t - _last_telem_ts >= 0.2:  # throttle to ~5 Hz
                _last_telem_ts = now_t
                try:
                    pwr_raw = struct.unpack_from('<H', raw, 14)[0]
                    temp_c = struct.unpack_from('<f', raw, 18)[0]
                    swr = struct.unpack_from('<f', raw, 26)[0]
                    # Approximate watts: linear above an idle baseline.
                    # Tunable — calibrate against a known TX power.
                    watts = max(0.0, (pwr_raw - TELEM_PWR_BASELINE)
                                * TELEM_PWR_SCALE)
                    asyncio.ensure_future(_send_ctrl(
                        f"getTXTelem:{watts:.1f},{swr:.2f},{temp_c:.0f},{pwr_raw}"))
                except Exception:
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
    out_len = int(len(arr) * 16000 / DSP_AUDIO_RATE)
    if out_len < 16: return
    out = np.interp(np.linspace(0, len(arr)-1, out_len),
                    np.arange(len(arr)), arr).astype(np.int16)
    frame = out.tobytes()
    dead = set()
    for ws in list(audio_rx_clients):
        try: await ws.send_bytes(frame)
        except: dead.add(ws)
    audio_rx_clients.difference_update(dead)


async def _broadcast_spectrum(spec):
    """Push one spectrum frame to waterfall clients as a compact uint8 array.

    spec: list of ~512 dB values clipped to [-120, 0]. We quantize each bin to
    a single byte (0 = -120 dB, 255 = 0 dB) — 512 bytes/frame, ~19 KB/s @ 38 Hz.
    The browser maps bytes back to a colour ramp for the waterfall row.
    """
    if not spectrum_clients: return
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
    await ws.accept(); spectrum_clients.add(ws)
    try:
        while True: await ws.receive()
    except (WebSocketDisconnect, RuntimeError): pass
    finally: spectrum_clients.discard(ws)


# ── WebSocket: Control (/WSCTRX) ──────────────────────────────────
@app.websocket("/WSCTRX")
async def ws_ctrl(ws: WebSocket):
    global recording_active, recording_buffer, recording_start_time
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
                elif cmd == "setPTT":
                    tx = val.lower() == "true"
                    await radio.set_ptt(tx)
                    if dsp_proc: dsp_proc.demodulator.set_ptt(tx)
                    await ws.send_text(f"getPTT:{str(tx).lower()}")
                elif cmd == "tune":
                    tune_on = val.lower() == "true"
                    await radio.set_tune(tune_on)
                    if dsp_proc and dsp_proc.modulator:
                        dsp_proc.modulator.activate_tune(tune_on)
                elif cmd == "setAFGain":
                    vol = float(val) / 100.0
                    if dsp_proc: dsp_proc.demodulator.set_volume(vol)
                    await radio.set_volume(vol)
                elif cmd == "setRFGain":
                    await radio.set_rf_gain(float(val)/100.0)
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
                        logger.info("Recording started")
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
    await ws.accept(); audio_tx_clients.add(ws)
    tx_rate = 16000  # mic sample rate from controls.js (48k downsampled ×3)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                # Int16 PCM mic frame → modulator queue
                if dsp_proc and dsp_proc.modulator and getattr(radio, '_ptt_active', False):
                    try:
                        dsp_proc.modulator.feed_audio(data, input_rate=tx_rate)
                    except Exception:
                        pass
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
                    if dsp_proc and dsp_proc.modulator:
                        dsp_proc.modulator.reset_mic()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
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
    logger.info(f"sunmrrc {scheme}://[::]:{WEB_PORT}")
    if ssl_cert:
        logger.info(f"TLS: {ssl_cert}")
        uvicorn.run(app, host="::", port=WEB_PORT, reload=False,
                    log_level="info", ssl_certfile=ssl_cert, ssl_keyfile=ssl_key)
    else:
        logger.warning("TLS 证书未找到,以 HTTP 启动 (iOS 无声音/无麦克风)")
        uvicorn.run(app, host="::", port=WEB_PORT, reload=False, log_level="info")

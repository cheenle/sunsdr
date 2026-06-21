"""
sunmrrc — SunSDR2 DX Mobile Web Control
========================================
"""
import asyncio, json, logging, os, struct, sys, time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "web_control"))
from sunsdr_direct import SunSDR2DXClient
from dsp import StreamProcessor, SpectrumProcessor, AudioDemodulator, AUDIO_RATE as DSP_AUDIO_RATE
import numpy as np

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, HTMLResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sunmrrc")

# ── Config ────────────────────────────────────────────────────────
DEVICE_HOST = os.environ.get("DEVICE_HOST", "192.168.16.200")
WEB_PORT = int(os.environ.get("WEB_PORT", "8081"))
STATIC_DIR = Path(__file__).parent / "static"

# ── App ───────────────────────────────────────────────────────────
radio = SunSDR2DXClient(host=DEVICE_HOST)
dsp_proc: StreamProcessor | None = None
iq_sock = None; tx_counter = 0x04B0; loop_counter = 0

# Connected clients
ctrl_clients: set[WebSocket] = set()
audio_rx_clients: set[WebSocket] = set()
audio_tx_clients: set[WebSocket] = set()
spectrum_clients: set[WebSocket] = set()

MIME = {".css":"text/css", ".js":"application/javascript", ".html":"text/html",
        ".json":"application/json", ".png":"image/png", ".wasm":"application/wasm"}


# ── Lifespan ──────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global dsp_proc
    ok = await radio.connect()
    logger.info(f"SunSDR2DX: {ok}")
    dsp_proc = StreamProcessor(
        spectrum=SpectrumProcessor(fft_size=2048),
        demodulator=AudioDemodulator())
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


# ── Static file serving (catch-all, must be first route) ──────────
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
    global iq_sock, tx_counter, loop_counter
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
    while radio.connected:
        if getattr(radio, '_ptt_active', False):
            tx_counter += 0x10000
            hdr = struct.pack("<HHIH", 0xFF32, 0xFFFD, tx_counter, 0x0102)
            try: iq_sock.sendto(hdr + b'\x00'*1200, ("192.168.16.200", 50002))
            except: pass
            await asyncio.sleep(0.0022); loop_counter += 1; continue

        # ── Send keep-alive / stream request even when idle ──────
        # The device needs periodic 0xFFFE packets to maintain the IQ stream.
        # Without this, the stream may stall and never start after boot.
        now = time.monotonic()
        if now - last_keepalive >= 0.5:
            tx_counter += 0x10000
            hdr = struct.pack("<HHIH", 0xFF32, 0xFFFE, tx_counter, 0x0001)
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
                p90 = float(np.percentile(spec, 90))
                s9 = max(0, min(60, int(9 + (p90 + 73)/6)))
                asyncio.ensure_future(_send_ctrl(f"getSignalLevel:{s9}"))
                if spectrum_clients:
                    asyncio.ensure_future(_broadcast_spectrum(spec))

            audio = dsp_proc.get_audio()
            if audio:
                audio_count += 1
                asyncio.ensure_future(_broadcast_audio(audio))

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
    for ws in ctrl_clients:
        try: await ws.send_text(msg)
        except: dead.add(ws)
    ctrl_clients.difference_update(dead)


async def _broadcast_audio(pcm: bytes):
    if not audio_rx_clients: return
    arr = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
    if len(arr) < 16: return
    out_len = int(len(arr) * 16000 / DSP_AUDIO_RATE)
    if out_len < 16: return
    out = np.interp(np.linspace(0, len(arr)-1, out_len),
                    np.arange(len(arr)), arr).astype(np.int16)
    frame = out.tobytes()
    dead = set()
    for ws in audio_rx_clients:
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
    for ws in spectrum_clients:
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
                    if dsp_proc: dsp_proc.demodulator.set_mode(mode)
                    await ws.send_text(f"getMode:{mode}")
                elif cmd == "setPTT":
                    tx = val.lower() == "true"
                    await radio.set_ptt(tx)
                    if dsp_proc: dsp_proc.demodulator.set_ptt(tx)
                    await ws.send_text(f"getPTT:{str(tx).lower()}")
                elif cmd == "tune":
                    await radio.set_tune(val.lower() == "true")
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
    await ws.accept(); audio_tx_clients.add(ws)
    try:
        while True: await ws.receive()
    except (WebSocketDisconnect, RuntimeError): pass
    finally: audio_tx_clients.discard(ws)


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

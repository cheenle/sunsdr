"""
SunSDR2 DX — Web Control Server
================================
Direct UDP backend only. No TCI/ExpertSDR3 required.

Architecture:
  Browser ←WebSocket→ FastAPI ←UDP→ SunSDR2 DX
   Web UI           :8080    :50001/:50002  Hardware
"""

import asyncio, json, logging, os, struct, sys, time, socket as sock_module
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, HTMLResponse, PlainTextResponse
import uvicorn

from sunsdr_direct import SunSDR2DXClient
from dsp import (StreamProcessor, SpectrumProcessor, AudioDemodulator,
                 AUDIO_RATE, decode_iq_24bit)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sunsdr_web")

# ── Config ────────────────────────────────────────────────────────
DEVICE_HOST = os.environ.get("DEVICE_HOST", "192.168.16.200")
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
STATIC_DIR = Path(__file__).parent / "static"

# ── App ───────────────────────────────────────────────────────────
app = FastAPI(title="SunSDR2 DX Web Control")
radio = SunSDR2DXClient(host=DEVICE_HOST)
dsp: StreamProcessor | None = None
web_clients: set[WebSocket] = set()

# ── Startup ───────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global dsp
    ok = await radio.connect()
    logger.info(f"Device connected: {ok}")
    dsp = StreamProcessor(
        spectrum=SpectrumProcessor(fft_size=2048),
        demodulator=AudioDemodulator())
    asyncio.create_task(_process_iq())
    asyncio.create_task(_heartbeat())


@app.on_event("shutdown")
async def shutdown():
    await radio.disconnect()


# ── Heartbeat (keep device session alive) ─────────────────────────
async def _heartbeat():
    while radio.connected:
        try:
            hb = struct.pack("<HHIIH", 0xFF32, 0x0018, 4, 0x00010000, 0)
            hb += struct.pack("<II", 0, 0)
            if radio._sock:
                radio._sock.sendto(hb, (radio.host, 50001))
        except Exception:
            pass
        await asyncio.sleep(0.5)


# ── IQ Processing ─────────────────────────────────────────────────
async def _process_iq():
    iq_sock = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_DGRAM)
    iq_sock.setblocking(False)
    iq_sock.setsockopt(sock_module.SOL_SOCKET, sock_module.SO_REUSEADDR, 1)
    iq_sock.bind(("192.168.16.100", 50002))
    logger.info("IQ: listening on 50002")

    loop = asyncio.get_event_loop()
    tx_ctr = 0x04B0
    pkt_count = 0

    while radio.connected:
        # PTT active → send modulated TX IQ (not silence!)
        if getattr(radio, '_ptt_active', False):
            tx_ctr += 0x10000
            # Get TX IQ data from modulator (700Hz test tone)
            tx_data = dsp.get_tx_iq() if dsp else b'\x00' * 1200
            if len(tx_data) < 1200:
                tx_data = tx_data + b'\x00' * (1200 - len(tx_data))
            hdr = struct.pack("<HHIH", 0xFF32, 0xFFFD, tx_ctr, 0x0102)
            try: iq_sock.sendto(hdr + tx_data[:1200], (DEVICE_HOST, 50002))
            except: pass
            await asyncio.sleep(0.0022)
            continue

        try:
            data = await asyncio.wait_for(
                loop.sock_recvfrom(iq_sock, 65536), timeout=0.5)
            raw = data[0]
            pkt_count += 1
        except (asyncio.TimeoutError, asyncio.CancelledError):
            continue
        except Exception:
            continue

        if len(raw) < 10 or raw[0] != 0x32 or raw[1] != 0xff:
            continue

        sub = struct.unpack('<H', raw[2:4])[0]

        if sub == 0xFFFE and len(raw) >= 1200:
            iq = decode_iq_24bit(raw[10:])
            if len(iq) >= 10:
                dsp.feed_iq(iq)

                # Spectrum + S-meter (once per FFT computation)
                if dsp.latest_spectrum is not None:
                    spec = dsp.latest_spectrum
                    dsp.latest_spectrum = None
                    p90 = float(np.percentile(spec, 90))
                    s9 = max(0, min(60, int(9 + (p90 + 73) / 6)))
                    asyncio.ensure_future(_broadcast_sensor(s9, round(p90, 1)))
                    asyncio.ensure_future(_broadcast({"type": "spectrum", "data": spec}))

                # Audio broadcast
                audio = dsp.get_audio()
                if audio:
                    asyncio.ensure_future(_broadcast_audio(audio))

        # TX keepalive
        if pkt_count % 100 == 0:
            tx_ctr += 0x10000
            hdr = struct.pack("<HHIH", 0xFF32, 0xFFFE, tx_ctr, 0x0001)
            try: iq_sock.sendto(hdr + b'\x00'*1200, (DEVICE_HOST, 50002))
            except: pass

        if pkt_count % 500 == 0:
            logger.info(f"IQ: {pkt_count} packets")

    iq_sock.close()


# ── Broadcast helpers ─────────────────────────────────────────────

async def _broadcast(msg: dict):
    if not web_clients: return
    payload = json.dumps(msg, default=str)
    dead = set()
    for ws in list(web_clients):
        try: await ws.send_text(payload)
        except: dead.add(ws)
    web_clients.difference_update(dead)

async def _broadcast_audio(pcm: bytes):
    if not web_clients: return
    hdr = struct.pack("<BBH", 0x01, 0, AUDIO_RATE // 100)
    frame = hdr + pcm
    dead = set()
    for ws in list(web_clients):
        try: await ws.send_bytes(frame)
        except: dead.add(ws)
    web_clients.difference_update(dead)

async def _broadcast_sensor(s9: int, dbm: float):
    await _broadcast({"type": "sensors", "s_meter": s9, "rx_dbm": dbm})

async def _push_state():
    await _broadcast({"type": "state", "data": radio.get_state()})


# ── WebSocket ─────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    web_clients.add(ws)
    try:
        await _push_state()
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            cmd = data.get("cmd", "")
            args = data.get("args", {})

            if cmd == "set_frequency":
                await radio.set_frequency(args.get("freq", 14074000))
            elif cmd == "set_mode":
                m = args.get("mode", "USB").upper()
                await radio.set_mode(m)
                if dsp: dsp.demodulator.set_mode(m)
            elif cmd == "set_ptt":
                tx = args.get("tx", False)
                await radio.set_ptt(tx)
                if dsp: dsp.demodulator.set_ptt(tx)
            elif cmd == "set_drive":
                await radio.set_drive(args.get("value", 50))
            elif cmd == "set_volume":
                v = args.get("value", 0.5)
                await radio.set_volume(v)
                if dsp: dsp.demodulator.set_volume(v)
            elif cmd == "set_preamp":
                await radio.set_preamp(args.get("on", False))
            elif cmd == "set_agc":
                await radio.set_agc_mode(args.get("mode", "AUTO"))
            elif cmd == "set_rf_gain":
                await radio.set_rf_gain(args.get("value", 1.0))
            elif cmd == "set_filter":
                lo = args.get("low", 200); hi = args.get("high", 2800)
                await radio.set_filter(lo, hi)
                if dsp: dsp.demodulator.reconfigure_filter(lo, hi)
            elif cmd == "set_rit":
                await radio.set_rit_enable(args.get("on", False))
            elif cmd == "set_split":
                await radio.set_split(args.get("on", False))
            elif cmd == "set_vfo_lock":
                await radio.set_vfo_lock(args.get("lock", False))
            elif cmd == "set_antenna":
                await radio.set_antenna(args.get("port", 1))
            elif cmd == "set_attenuator":
                await radio.set_attenuator(args.get("db", 0))
            elif cmd == "get_state":
                await _push_state()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS: {e}")
    finally:
        web_clients.discard(ws)


# ── REST API ──────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    return {"backend": "direct", "connected": radio.connected,
            "rx_freq": int(radio.rx_freq), "mode": radio.mode}

@app.post("/api/ptt/{state}")
async def api_ptt(state: str):
    tx = state.lower() in ("on", "true", "1")
    await radio.set_ptt(tx)
    return {"ok": True, "ptt": tx}


# ── Static files ──────────────────────────────────────────────────

@app.get("/")
async def index():
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

@app.get("/{filename:path}")
async def static_file(filename: str):
    fp = STATIC_DIR / filename
    if fp.is_file():
        ext = Path(filename).suffix.lower()
        mime = {".css":"text/css", ".js":"application/javascript",
                ".html":"text/html", ".json":"application/json",
                ".png":"image/png", ".wasm":"application/wasm"}
        return Response(fp.read_bytes(), media_type=mime.get(ext, "text/plain"))
    return PlainTextResponse("", status_code=404)


# ── Main ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info(f"SunSDR2 DX Web Control on {WEB_HOST}:{WEB_PORT}")
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level="info")

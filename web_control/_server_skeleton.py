# Source Generated with Decompyle++
# File: server.cpython-312.pyc (Python 3.12)

'''
SunSDR Web Control Server v3
=============================
Dual-backend server: TCI (via ExpertSDR3) or DIRECT (UDP to hardware).

Startup:
  BACKEND=tci    ./start.sh    # Via ExpertSDR3 TCI
  BACKEND=direct ./start.sh    # Direct UDP to hardware
'''
import asyncio
import json
import logging
import os
import sys
import struct
from pathlib import Path
for v in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'SOCKS_PROXY', 'socks_proxy', 'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy'):
    os.environ.pop(v, None)
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'
from dsp import AUDIO_RATE
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import uvicorn
logging.basicConfig(level = logging.INFO, format = '%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('sunsdr_web')
BACKEND = os.environ.get('BACKEND', 'tci').lower()
WEB_HOST = os.environ.get('WEB_HOST', '0.0.0.0')
WEB_PORT = int(os.environ.get('WEB_PORT', '8080'))
STATIC_DIR = Path(__file__).parent / 'static'
radio = None
backend_info = {
    'type': BACKEND,
    'connected': False,
    'ready': False }
if BACKEND == 'direct':
    from sunsdr_direct import SunSDR2DXClient
    DEVICE_HOST = os.environ.get('DEVICE_HOST', '192.168.16.200')
    DEVICE_PORT = int(os.environ.get('DEVICE_PORT', '50001'))
    radio = SunSDR2DXClient(host = DEVICE_HOST, control_port = DEVICE_PORT)
    logger.info(f'''DIRECT backend: {DEVICE_HOST}:{DEVICE_PORT}''')
else:
    from tci_client import TCIClient, Modulation, AGCMode
    TCI_HOST = os.environ.get('TCI_HOST', '127.0.0.1')
    TCI_PORT = int(os.environ.get('TCI_PORT', '50001'))
    radio = TCIClient(host = TCI_HOST, port = TCI_PORT)
    logger.info(f'''TCI backend: {TCI_HOST}:{TCI_PORT}''')
app = FastAPI(title = 'SunSDR Web Control', version = '3.0.0')
web_clients: set[WebSocket] = set()
listen_task: asyncio.Task | None = None
_active_demodulator = None

async def broadcast(msg = None):
    pass
# WARNING: Decompyle incomplete


async def broadcast_audio(pcm = None):
    '''Broadcast raw 16-bit PCM audio as a binary WebSocket frame.

    Frame layout: [1 byte type=0x01][1 byte pad][2 bytes sampleRate/100 LE][PCM int16 LE...]
    The 4-byte header keeps the PCM payload 2-byte aligned so the browser can
    wrap it in an Int16Array without a RangeError. Sending binary avoids the
    ~4x bloat of JSON number arrays and keeps the audio path off the text
    broadcast queue so it stays low-latency.
    '''
    pass
# WARNING: Decompyle incomplete


async def push_state():
    '''Read current radio state and push to all web clients.'''
    pass
# WARNING: Decompyle incomplete

startup = (lambda : pass# WARNING: Decompyle incomplete
)()

async def _set_backend_status(connected = None, ready = None):
    pass
# WARNING: Decompyle incomplete


async def _direct_poll():
    '''Poll state + send heartbeats to keep device alive.'''
    pass
# WARNING: Decompyle incomplete


async def _process_iq_stream():
    '''Process IQ stream from port 50002, broadcast spectrum + audio.'''
    pass
# WARNING: Decompyle incomplete

shutdown = (lambda : pass# WARNING: Decompyle incomplete
)()
api_status = (lambda : pass# WARNING: Decompyle incomplete
)()
api_state = (lambda : pass# WARNING: Decompyle incomplete
)()
api_ptt = (lambda state = app.get('/api/status'): pass# WARNING: Decompyle incomplete
)()
ws_endpoint = (lambda ws = None: pass# WARNING: Decompyle incomplete
)()

async def _handle_direct(ws, cmd, args, rx):
    '''Route commands to direct UDP backend.'''
    pass
# WARNING: Decompyle incomplete


async def _handle_tci(ws, cmd, args, rx):
    '''Route commands to TCI backend.'''
    pass
# WARNING: Decompyle incomplete

index = (lambda : pass# WARNING: Decompyle incomplete
)()
app.mount('/static', StaticFiles(directory = str(STATIC_DIR)), name = 'static')
if __name__ == '__main__':
    logger.info(f'''SunSDR Web Control v3 on {WEB_HOST}:{WEB_PORT} [{BACKEND.upper()}]''')
    uvicorn.run('server:app', host = WEB_HOST, port = WEB_PORT, reload = False, log_level = 'info')
    return None

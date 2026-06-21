"""
SunSDR2 DX Direct UDP Protocol Library
=======================================
Verified against ExpertSDR3 1.0.17 packet captures.

Packet format (14-byte header + trailing word):
  [0-1] uint16 magic  = 0xFF32
  [2-3] uint16 cmd_id
  [4-7] uint32 data_len
  [8-11] uint32 index   = 0x00010000 (primary TRX)
  [12-13] uint16 reserved = 0x0000
  [14:14+N] payload
  [14+N:14+N+4] trailing word (not CRC; meaning varies by command)

Key rules:
  - All control packets must originate from PC port 50001
  - Heartbeat (0x0018) every 0.5s keeps session alive (~8min timeout)
  - 0x0006 PTT: payload=0, trailing word=1(TX)/0(RX)
  - 0x0002 MUST be first command (Start Operation / power-on)
  - Device IP: 192.168.16.200, control :50001, stream :50002
"""

import asyncio, logging, socket, struct, time, zlib
from dataclasses import dataclass
from typing import Optional, Callable

logger = logging.getLogger(__name__)

MAGIC = 0xFF32
DEVICE_HOST = "192.168.16.200"
LOCAL_HOST = "192.168.16.100"
CTRL_PORT = 50001
IQ_PORT = 50002
IF_OFFSET = 30500.0   # RX DDS = VFO + IF_OFFSET (verified from pcap)
SESSION_ID = 0x04B0   # low 16 bits of stream counter

# ── Command IDs ────────────────────────────────────────────────────

class CmdID:
    START_OPS    = 0x0002  # Start Operation (power-on, MUST be first)
    HW_INIT      = 0x0001  # Hardware init (68 bytes)
    SET_PARAM_5  = 0x0005  # Parameter set (mode/att/preamp)
    PTT          = 0x0006  # PTT: payload=0, trailing word=1(TX)/0(RX)
    SET_PARAM_7  = 0x0007  # Extended config (44 bytes)
    RX_FREQ      = 0x0008  # RX DDS frequency (IF offset included)
    TX_FREQ      = 0x0009  # TX frequency (exact VFO)
    INFO_QUERY   = 0x000E  # Device info (486-byte response)
    CALIB_QUERY  = 0x0010  # Calibration data (486-byte response)
    SET_PARAM_15 = 0x0015  # Parameter set
    HEARTBEAT    = 0x0018  # Keepalive (every 0.5s)
    SET_PARAM_19 = 0x0019  # Parameter set
    SET_PARAM_1B = 0x001B  # Parameter set
    SET_PARAM_1D = 0x001D  # Parameter set
    SET_PARAM_1E = 0x001E  # Parameter set
    STREAM_CTRL  = 0x0020  # Stream config (70 bytes)
    SET_PARAM_21 = 0x0021  # Parameter set
    SET_PARAM_22 = 0x0022  # Parameter set (30 bytes)
    SET_PARAM_24 = 0x0024  # Parameter set
    SET_PARAM_26 = 0x0026  # Parameter set
    VOX_CTRL     = 0x0027  # VOX config (34 bytes)
    STATUS       = 0x005A  # Status query (28-byte response)
    PRE_CONFIG   = 0x005F  # Pre-boot config (24 bytes)


# ── Packet builder ────────────────────────────────────────────────

def build_packet(cmd_id: int, data: bytes = b"",
                 index: int = 0x00010000,
                 trailing: int = 0) -> bytes:
    """Build a 14-byte header + payload + 4-byte trailing word."""
    hdr = struct.pack("<HHIIH", MAGIC, cmd_id, len(data), index, 0)
    return hdr + data + struct.pack("<I", trailing)


# ── Client ────────────────────────────────────────────────────────

class SunSDR2DXClient:
    """Direct UDP client for SunSDR2 DX hardware."""

    def __init__(self, host: str = DEVICE_HOST):
        self.host = host
        self._sock: Optional[socket.socket] = None
        self._connected = False

        # State
        self.rx_freq: float = 7_074_000.0
        self.tx_freq: float = 7_074_000.0
        self.mode: str = "USB"
        self.ptt: bool = False
        self.drive: int = 50
        self.volume: float = 0.5
        self.preamp: bool = False
        self.attenuator: int = 0
        self.agc_mode: str = "AUTO"
        self.rf_gain: float = 1.0
        self.filter_low: int = 200
        self.filter_high: int = 2800
        self.rit_enable: bool = False
        self.rit_offset: int = 0
        self.split_enable: bool = False
        self.vfo_lock: bool = False
        self.antenna: int = 1
        self.serial: str = ""

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Connection ─────────────────────────────────────────────

    async def connect(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((LOCAL_HOST, CTRL_PORT))
            logger.info("Bound to port 50001")
            await self._send_boot_sequence()
            self._connected = True
            logger.info("Device initialized and operational")
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False

    async def disconnect(self):
        self._connected = False
        if self._sock:
            self._sock.close()
            self._sock = None

    async def _send_raw(self, data: bytes):
        if self._sock:
            self._sock.sendto(data, (self.host, CTRL_PORT))

    async def _send_boot_sequence(self):
        """Complete boot sequence — verified byte-for-byte against ExpertSDR3 pcap."""
        # Use EXACT hex from pcap for all boot commands (no dynamic building)
        boot_hex = [
            # Start Operation + pre-config
            "32ff0200040000000000010000000000000000000000",
            "32ff5f000600000000000100000000000000000000000000",
            "32ff5f000600000000000100000000000000000000000000",
            "32ff5f000600000000000100000000000000000000000000",
            "32ff1d00040000000000010000000000000000000000",
            "32ff1b00040000000000010000000000000000000000",
            "32ff0500040000000000010000000000000002000000",
            "32ff1800040000000000010000000000000000000000",
            "32ff19000400000000000100000000000000bb000000",
            "32ff2100040000000000010000000000000001000000",
            # Status queries
            "32ff5a000000000000000100000000000000",
            "32ff5a000000000000000100000000000000",
            "32ff5a000000000000000100000000000000",
            "32ff5a000000000000000100000000000000",
            "32ff5a000000000000000100000000000000",
            # HW init
            "32ff01003200000000000100000000000000320000003200000032000000320000003200000032000000320000003200000000000000010001000100000000000000c025",
        ]
        for h in boot_hex:
            await self._send_raw(bytes.fromhex(h))
            await asyncio.sleep(0.03)

        # Frequencies (dynamic — need current VFO)
        dds = int((self.rx_freq + IF_OFFSET) * 10)
        vfo = int(self.rx_freq * 10)
        freq_cmds = [
            build_packet(CmdID.TX_FREQ, struct.pack("<II", 0, vfo)),
            build_packet(CmdID.RX_FREQ, struct.pack("<II", 0, dds)),
            build_packet(CmdID.RX_FREQ, struct.pack("<II", 0, dds)),
        ]
        for cmd in freq_cmds:
            await self._send_raw(cmd)
            await asyncio.sleep(0.03)

        # Post-init + stream start (exact pcap hex)
        post_hex = [
            "32ff17000400000000000100000000000000dc000000",
            "32ff1e00040000000000010000000000000000000000",
            "32ff1500040000000000010000000000000001000000",
            "32ff07001a000000000001000000000000000000000000000000000000000000000000000000000000000000",
            "32ff2400040000000000010000000000000000000000",
            "32ff20003400000000000100000000000000000000000100000000000000000000006400000000000000000000001e000000bc02000007000000640000002c01000064000000",
            "32ff1800040000000000010000000000000000000000",
            "32ff2600040000000000010000000000000000000000",
            "32ff27001000000000000100000000000000dc460300b6d20000dc460300b6d20000",
            "32ff22000c00000000000100000000000000000000000084d71700000000",
        ]
        for h in post_hex:
            await self._send_raw(bytes.fromhex(h))
            await asyncio.sleep(0.03)

        logger.info("Boot sequence complete")

    # ── Frequency ──────────────────────────────────────────────

    async def set_rx_frequency(self, freq_hz: float):
        self.rx_freq = freq_hz
        dds = int((freq_hz + IF_OFFSET) * 10)
        await self._send_raw(
            build_packet(CmdID.RX_FREQ, struct.pack("<II", 0, dds)))

    async def set_tx_frequency(self, freq_hz: float):
        self.tx_freq = freq_hz
        await self._send_raw(
            build_packet(CmdID.TX_FREQ, struct.pack("<II", 0, int(freq_hz * 10))))

    async def set_frequency(self, freq_hz: float):
        await self.set_rx_frequency(freq_hz)
        await self.set_tx_frequency(freq_hz)

    # ── PTT ────────────────────────────────────────────────────

    async def set_ptt(self, tx: bool):
        """PTT: payload=0, trailing word=1(TX)/0(RX)."""
        self.ptt = tx
        self._ptt_active = tx
        await self._send_raw(
            build_packet(CmdID.PTT, struct.pack("<I", 0), trailing=1 if tx else 0))

    # ── Mode / Filter / AGC ───────────────────────────────────

    async def set_mode(self, mode: str):
        self.mode = mode.upper()

    async def set_filter(self, low_hz: int, high_hz: int):
        self.filter_low = low_hz
        self.filter_high = high_hz

    async def set_agc_mode(self, mode: str):
        self.agc_mode = mode.upper()

    # ── Gain / Preamp / Attenuator ─────────────────────────────

    async def set_volume(self, value: float):
        self.volume = max(0.0, min(1.0, value))

    async def set_drive(self, value: int):
        self.drive = max(0, min(100, value))

    async def set_rf_gain(self, value: float):
        self.rf_gain = max(0.0, min(1.0, value))

    async def set_preamp(self, enable: bool):
        self.preamp = enable

    async def set_attenuator(self, db: int):
        self.attenuator = db

    async def set_antenna(self, port: int):
        self.antenna = max(1, min(3, port))

    async def set_tune(self, enable: bool):
        """Tune mode: engage PTT for carrier/wav playback through TX chain."""
        self._tune_active = enable
        await self.set_ptt(enable)

    # ── RIT / Split / Lock ─────────────────────────────────────

    async def set_rit_enable(self, enable: bool):
        self.rit_enable = enable

    async def set_rit_offset(self, offset_hz: int):
        self.rit_offset = offset_hz

    async def set_split(self, enable: bool):
        self.split_enable = enable

    async def set_vfo_lock(self, lock: bool):
        self.vfo_lock = lock

    # ── State ──────────────────────────────────────────────────

    def get_state(self) -> dict:
        return {
            "connected": self._connected, "serial": self.serial,
            "rx_freq": int(self.rx_freq), "tx_freq": int(self.tx_freq),
            "freq_mhz": f"{self.rx_freq / 1_000_000:.6f}",
            "mode": self.mode, "ptt": self.ptt, "drive": self.drive,
            "volume": round(self.volume, 2), "preamp": self.preamp,
            "attenuator": self.attenuator, "agc_mode": self.agc_mode,
            "rf_gain": round(self.rf_gain, 2),
            "filter_low": self.filter_low, "filter_high": self.filter_high,
            "rit_enable": self.rit_enable, "rit_offset": self.rit_offset,
            "split_enable": self.split_enable, "vfo_lock": self.vfo_lock,
            "antenna": self.antenna,
        }

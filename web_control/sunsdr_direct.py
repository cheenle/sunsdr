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

# ── Spectrum / IQ sample rate ──────────────────────────────────────
# ── IQ Sample Rates ─────────────────────────────────────────────────
# The SunSDR2 DX supports 4 IQ sample rates, multiples of 5^7 (78125):
#   39 kHz  = 78125 / 2     (5^7 / 2)
#   78 kHz  = 78125         (5^7, the verified default)
#   156 kHz = 78125 * 2
#   312 kHz = 78125 * 4
#
# The rate is selected by 0x0001 HW_INIT word[11] (NOT 0x0020 STREAM_CTRL):
#   word[11]=0 → 39k, 1→78k, 2→156k, 3→312k
# Verified 2025-06-24 via ExpertSDR3 capture + direct device test.
# See PROTOCOL.md §4.3 for full payload decode.

SAMPLE_RATES: dict[str, int] = {
    "39k":  78125 // 2,    # 39062
    "78k":  78125,         # default, verified
    "156k": 78125 * 2,     # 156250
    "312k": 78125 * 4,     # 312500
}

# 0x0001 HW_INIT word[11] rate index mapping
HW_INIT_RATE_INDEX: dict[str, int] = {
    "39k": 0,
    "78k": 1,
    "156k": 2,
    "312k": 3,
}

# 0x0020 STREAM_CTRL fixed payload (13 u32 LE words — rate-independent)
# word[1] = 0 matches the original 78k capture that had working TX.
# ExpertSDR3 uses word[1]=1, but changing it to 1 killed TX power on SunSDR2.
STREAM_CTRL_WORDS = [0, 0, 1, 0, 0, 100, 0, 0, 30, 700, 7, 100, 300]
STREAM_CTRL_TRAILING = 0x64  # 100


def build_stream_ctrl() -> bytes:
    """Build the 0x0020 STREAM_CTRL packet (rate-independent, always the same)."""
    payload = struct.pack("<%dI" % len(STREAM_CTRL_WORDS), *STREAM_CTRL_WORDS)
    return build_packet(CmdID.STREAM_CTRL, payload, trailing=STREAM_CTRL_TRAILING)


def build_hw_init(rate_key: str = "78k") -> bytes:
    """Build 0x0001 HW_INIT with the IQ sample rate encoded in word[11].

    word[11] = rate index: 0=39k, 1=78k, 2=156k, 3=312k
    word[10] = word[11] * 65536 + 1  (redundant encoding)
    Payload is exactly 50 bytes (12 words + 2B zero pad) to match ExpertSDR3.
    Trailing is fixed 0x509E9C00 (verified from ExpertSDR3 capture).
    """
    rate_index = HW_INIT_RATE_INDEX.get(rate_key, 1)  # default 78k
    words = [
        0, 50, 50, 50, 50, 50, 50, 50, 50, 0,          # calibration params
        rate_index * 65536 + 1,                           # word[10]: rate high
        rate_index,                                       # word[11]: ★ rate index ★
    ]
    payload = struct.pack("<%dI" % len(words), *words) + b'\x00\x00'
    return build_packet(CmdID.HW_INIT, payload, trailing=0x509E9C00)

# ── Per-band TX power (drive %) ─────────────────────────────────────
# EDIT THESE to set max output power per band. drive % maps to the 0x0017
# byte via a square-root taper: byte = round(255 * sqrt(drive/100)).
#
# Defaults are derived from the band-calibration values ExpertSDR3 wrote
# at each frequency (captured via TCI): byte -> drive% = (byte/255)^2 * 100
#   3.5 MHz  0xC8(200) -> 62%      14 MHz  0xDC(220) -> 74%
#   7   MHz  0xFF(255) -> 100%     21 MHz  0xC5(197) -> 60%
#                                  28 MHz  0xDA(218) -> 73%
# Each entry: (low_hz, high_hz, drive_percent). First matching range wins.
# Frequencies outside every range fall back to BAND_POWER_DEFAULT.
#
# This is a runtime-editable setting, NOT a hardcoded constant: the server
# loads band_power.json on startup and calls set_band_power() to override this
# table, and the /api/band_power endpoint lets the frontend read/write it.
# The list below is only the fallback default when no JSON config exists.
BAND_POWER = [
    (1_800_000,   2_000_000,   100),  # 160m
    (3_500_000,   4_000_000,    62),  # 80m
    (5_000_000,   5_500_000,    80),  # 60m
    (7_000_000,   7_300_000,   100),  # 40m
    (10_100_000,  10_150_000,   90),  # 30m
    (14_000_000,  14_350_000,   74),  # 20m
    (18_068_000,  18_168_000,   80),  # 17m
    (21_000_000,  21_450_000,   60),  # 15m
    (24_890_000,  24_990_000,   70),  # 12m
    (28_000_000,  29_700_000,   73),  # 10m
]
BAND_POWER_DEFAULT = 80   # drive % for frequencies not in any band above


def set_band_power(table, default=None):
    """Replace the per-band power table at runtime (called by the server after
    loading band_power.json). `table` is a list of (low_hz, high_hz, pct)."""
    global BAND_POWER, BAND_POWER_DEFAULT
    if table is not None:
        BAND_POWER = [(int(lo), int(hi), int(pct)) for lo, hi, pct in table]
    if default is not None:
        BAND_POWER_DEFAULT = int(default)


def band_power_for(freq_hz: float) -> int:
    """Return the configured drive % for the band containing freq_hz."""
    for lo, hi, pct in BAND_POWER:
        if lo <= freq_hz <= hi:
            return pct
    return BAND_POWER_DEFAULT


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
    DRIVE        = 0x0017  # TX drive/power. uint32 byte = 255*(drive%/100)^0.5
                           # (verified via TCI capture: 10%->0x50, 50%->0xb4,
                           # 100%->0xff). Square-root taper. ExpertSDR3 also
                           # sends this per-band at freq change (band power cal).
    HEARTBEAT    = 0x0018  # Keepalive (every 0.5s)
    SET_PARAM_19 = 0x0019  # Parameter set
    SET_PARAM_1B = 0x001B  # Parameter set
    SET_PARAM_1D = 0x001D  # Parameter set
    SET_PARAM_1E = 0x001E  # Parameter set
    HW_INIT      = 0x0001  # Hardware init + IQ sample rate selector
    STREAM_CTRL  = 0x0020  # Stream config (52 bytes, rate-independent)
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
        self.drive: int = 100
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
        self.sample_rate_key: str = "78k"   # spectrum/IQ rate selector

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
        """Complete boot sequence — verified byte-for-byte against ExpertSDR3 pcap.

        The only dynamic element is 0x0001 HW_INIT (word[11] = IQ sample rate index).
        All other commands are fixed hex from the capture.
        """
        # Phase 1-2: Start Operation + pre-config (fixed hex)
        boot_hex = [
            "32ff0200040000000000010000000000000000000000",
            "32ff5f000600000000000100000000000000000000000000",
            "32ff5f000600000000000100000000000000000000000000",
            "32ff5f000600000000000100000000000000000000000000",
            "32ff1d00040000000000010000000000000000000000",
            "32ff1b00040000000000010000000000000000000000",
            "32ff0500040000000000010000000000000001000000",  # trail=1 matches ExpertSDR3 boot
            "32ff1800040000000000010000000000000000000000",
            "32ff19000400000000000100000000000000bb000000",
            "32ff2100040000000000010000000000000001000000",
            # Status queries
            "32ff5a000000000000000100000000000000",
            "32ff5a000000000000000100000000000000",
            "32ff5a000000000000000100000000000000",
            "32ff5a000000000000000100000000000000",
            "32ff5a000000000000000100000000000000",
        ]
        for h in boot_hex:
            await self._send_raw(bytes.fromhex(h))
            await asyncio.sleep(0.03)

        # Phase 3: HW_INIT (0x0001) — dynamic: encodes IQ sample rate
        await self._send_raw(build_hw_init(self.sample_rate_key))
        await asyncio.sleep(0.03)

        # Phase 4: Frequencies (dynamic — need current VFO)
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

        # Phase 5: Post-init + stream start (fixed hex)
        post_hex = [
            "32ff17000400000000000100000000000000dc000000",
            "32ff1e00040000000000010000000000000000000000",
            "32ff1500040000000000010000000000000001000000",
            "32ff07001a000000000001000000000000000000000000000000000000000000000000000000000000000000",
            "32ff2400040000000000010000000000000000000000",
        ]
        for h in post_hex:
            await self._send_raw(bytes.fromhex(h))
            await asyncio.sleep(0.03)

        # ★ 0x0020 STREAM_CTRL — fixed, rate-independent
        await self._send_raw(build_stream_ctrl())
        await asyncio.sleep(0.03)

        # Post-stream commands
        post2_hex = [
            "32ff1800040000000000010000000000000000000000",
            "32ff2600040000000000010000000000000000000000",
            "32ff27001000000000000100000000000000dc460300b6d20000dc460300b6d20000",
            "32ff22000c00000000000100000000000000000000000084d71700000000",
        ]
        for h in post2_hex:
            await self._send_raw(bytes.fromhex(h))
            await asyncio.sleep(0.03)

        logger.info("Boot sequence complete (rate=%s, word[11]=%d)",
                     self.sample_rate_key,
                     HW_INIT_RATE_INDEX.get(self.sample_rate_key, 1))

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
        # ExpertSDR3 re-sends 0x0017 (drive) on every band/frequency change —
        # the device resets it to a per-band calibration value otherwise. Mirror
        # that, but use OUR configurable per-band power (BAND_POWER) so each band
        # transmits at the level the user set (verified via TCI capture).
        self.drive = band_power_for(freq_hz)
        await self._send_drive_byte()

    # ── PTT ────────────────────────────────────────────────────

    async def set_ptt(self, tx: bool):
        """PTT: payload=0, trailing word=1(TX)/0(RX).

        On TX assert, (re)send the current drive (0x0017) first — ExpertSDR3
        does the same, and it guards against the device having reset 0x0017 to
        a band-calibration value on the last frequency change. Without this the
        far end heard very low power because drive was stuck at the boot byte."""
        if tx:
            await self._send_drive_byte()
        self.ptt = tx
        self._ptt_active = tx
        await self._send_raw(
            build_packet(CmdID.PTT, struct.pack("<I", 0), trailing=1 if tx else 0))

    # ── Sample rate (spectrum/IQ width) ────────────────────────

    async def set_sample_rate(self, key: str) -> bool:
        """Switch the spectrum/IQ sample rate (39k/78k/156k/312k).

        Requires a full re-boot because the rate is encoded in 0x0001 HW_INIT
        word[11] which must be sent BEFORE frequency and stream-start commands.
        See PROTOCOL.md §4.3 for the word[11] mapping.

        Returns True if the key is valid and reboot was triggered.
        """
        if key not in SAMPLE_RATES:
            logger.warning("Unknown sample rate key: %s", key)
            return False

        old_key = self.sample_rate_key
        self.sample_rate_key = key
        logger.info("Sample rate: %s → %s (%d Hz), rebooting...",
                     old_key, key, SAMPLE_RATES[key])

        # Full re-boot with the new rate encoded in 0x0001
        await self._send_boot_sequence()
        logger.info("Sample rate switch complete: %s", key)
        return True

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
        """Set TX drive/power 0..100%. Sends 0x0017 to the device with a
        square-root taper byte (verified via TCI capture against ExpertSDR3):

            byte = round(255 * sqrt(drive/100))

        Mapping: 10%->0x50, 30%->0x8b, 50%->0xb4, 70%->0xd5, 90%->0xf1,
        100%->0xff. This is the SAME field ExpertSDR3 writes per-band at
        frequency change (band power calibration), so 100% == that band's
        factory-safe ceiling — it will not overdrive the PA.

        Previously this only set self.drive locally and never told the device,
        so drive was stuck at the boot value (0xDC) regardless of the UI."""
        self.drive = max(0, min(100, value))
        await self._send_drive_byte()

    async def _send_drive_byte(self):
        """Send the current self.drive as a 0x0017 command (sqrt taper byte).

        WIRE FORMAT (verified byte-for-byte against TCI capture):
            32ff1700 04000000 00000100 0000  00000000  dc000000
            └ header (cmd=0x17, len=4) ────┘ └ data=0 ┘ └ trailing=drive ┘
        The drive byte lives in the TRAILING word, NOT the data payload.
        data is 4 zero bytes; trailing carries the value. (Earlier bug put
        the byte in data with trailing=0 → device read drive=0 → W=0.0.)"""
        byte = int(round(255 * (self.drive / 100.0) ** 0.5))
        byte = max(0, min(255, byte))
        await self._send_raw(
            build_packet(CmdID.DRIVE, struct.pack("<I", 0), trailing=byte))

    async def set_rf_gain(self, value: float):
        self.rf_gain = max(0.0, min(1.0, value))

    async def set_preamp(self, enable: bool):
        self.preamp = enable

    async def set_attenuator(self, level: int):
        """Set hardware ATT/preamp via 0x0005 SET_PARAM_5 trailing word.

        Verified against ExpertSDR3 capture (2025-06-24):
          trail=0 → -20 dB attenuator
          trail=1 → -10 dB attenuator
          trail=2 → 0 dB (bypass)
          trail=3 → +10 dB preamp
        """
        level = max(0, min(3, int(level)))
        self.attenuator = level
        await self._send_raw(
            build_packet(CmdID.SET_PARAM_5, struct.pack("<I", 0), trailing=level))
        labels = {-2: "-20dB", -1: "-10dB", 0: "0dB", 1: "+10dB"}
        label = labels.get(level - 2, f"{level}")
        logger.info("ATT/Preamp: %s (trail=%d)", label, level)

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

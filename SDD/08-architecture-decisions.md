# 8. Architecture Decisions (ART 0513)

## AD-001: Use FastAPI/Uvicorn for SunMRRC Server

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | Use FastAPI with native WebSocket routes and Uvicorn runtime |

**Problem**: The SunMRRC service needs static file serving, multiple WebSocket endpoints, async UDP processing, and simple TLS startup in a small Python process.

**Rationale**: FastAPI/Uvicorn provides direct async integration and a small code surface. The current backend does not require the older MRRC Tornado structure.

**Consequences**: Documentation, service model, and operational scripts must describe FastAPI/Uvicorn, not Tornado.

## AD-002: Control SunSDR2 DX Directly over UDP

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | Use `SunSDR2DXClient` from `../web_control/sunsdr_direct.py` |

**Problem**: SunSDR2 DX exposes a hardware-specific UDP protocol; generic rig abstractions do not cover IQ stream and device-specific startup behavior.

**Rationale**: The shared direct client contains verified command IDs, packet builders, boot sequence, and radio setters.

**Consequences**: The app is SunSDR2 DX oriented. It is not currently a generic Hamlib application.

## AD-003: Treat Mode as Software DSP State

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | `setMode` updates the demodulator; hardware remains IQ/mode-agnostic in this path |

**Problem**: The hardware stream provides IQ; demodulation mode is selected in software.

**Rationale**: Keeping mode in the demodulator gives immediate browser RX behavior without depending on a hardware mode command.

**Consequences**: UI `getMode` reflects DSP state, not necessarily a persistent hardware-side mode.

## AD-004: Tagged Dual-Codec Audio Transport (Opus + Int16 PCM, RX and TX)

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | Both `/WSaudioRX` (server→client) and `/WSaudioTX` (client→server) carry a 1-byte codec tag per frame: `0x00` = 16 kHz Int16 PCM, `0x01` = Opus (16 kHz mono). Default Opus; runtime-switchable via `setOpus:` (Audio Codec menu). |

**Problem**: (RX) Int16 PCM is reliable but costs ~256 kbit/s — heavy on mobile links. Opus cuts that to ~18–24 kbit/s (>10×). (TX) The Opus→PCM transition in the TX uplink introduced ambiguity without a tag. A 1-byte per-frame codec tag removes all negotiation races — the receiver inspects the tag and decodes accordingly, so PCM and Opus can be switched mid-stream with no handshake.

**Rationale**: Unified codec-tagging for both RX and TX paths. Server-side RX encoder uses `web_control/opus_rx.py` (direct ctypes libopus bindings, bitrate via `max_data_bytes` cap on `opus_encode()` to avoid arm64 variadic `opus_encoder_ctl` issues). Server-side TX decoder uses the same libopus for `opus_decode()`. Frontend RX uses WASM `OpusDecoder` for decode; frontend TX uses WASM `OpusEncoder` with tag-byte prepended.

**Consequences**: Both RX and TX bandwidth drop >10× on mobile. Adds a libopus dependency on the server (optional — degrades gracefully to PCM). Codec is user-selectable per session. `AUDIO_TAG_PCM` / `AUDIO_TAG_OPUS` are global constants in the frontend, shared by RX decode and TX encode paths.

## AD-005: Quantize Spectrum Frames Server-Side

| Attribute | Value |
|-----------|-------|
| Type | Performance |
| Status | Implemented |
| Decision | Convert dB spectrum to uint8 bytes before `/WSspectrum` broadcast |

**Problem**: Spectrum frames must be frequent without excessive bandwidth.

**Rationale**: A 512-bin uint8 row is small and simple for canvas rendering.

**Consequences**: Browser receives display-oriented data, not high-precision spectrum values.

## AD-006: Prefer HTTPS/WSS by Default for Mobile

| Attribute | Value |
|-----------|-------|
| Type | Operational/Security |
| Status | Implemented |
| Decision | Auto-enable TLS when cert/key files are present |

**Problem**: iOS Safari does not expose reliable microphone and AudioContext behavior to non-secure origins.

**Rationale**: HTTPS is required for the real mobile operating environment.

**Consequences**: Local HTTP is explicitly a debug mode via missing certs or `DISABLE_SSL=1`.

## AD-007: Build PTT Release as Safety-Critical Flow

| Attribute | Value |
|-----------|-------|
| Type | Safety |
| Status | Implemented |
| Decision | Frontend release ACK retry, backup command, watchdog, and backend forced-RX handler |

**Problem**: A lost `setPTT:false` can leave the radio transmitting.

**Rationale**: Release is more safety-critical than keying. Multiple paths reduce stuck-TX risk.

**Consequences**: Frontend PTT logic is more complex, but deliberately so.

## AD-008: Keep WDSP Optional and Runtime-Toggled

| Attribute | Value |
|-----------|-------|
| Type | Extensibility |
| Status | Implemented |
| Decision | Initialize WDSP when available; setters remain safe when unavailable |

**Problem**: `libwdsp` availability varies by machine.

**Rationale**: RX should still work without WDSP; UI can query availability.

**Consequences**: Some DSP controls are conditional and must be reflected in UI/status.

## AD-009: Preserve Frontend Hooks but Document Missing Backends

| Attribute | Value |
|-----------|-------|
| Type | Product/Architecture |
| Status | Active |
| Decision | Do not claim ATR or CW/FT8 pages as complete until backends exist; TX modulation and the memory-channel API are now implemented and no longer gated |

**Problem**: The frontend contains inherited/planned hooks from broader MRRC work.

**Rationale**: Removing them may be premature, but overstating them causes design drift.

**Consequences**: SDD explicitly separates implemented, transport-only, and planned capabilities. As of 2026-06, TX voice modulation and `/api/mem_channels` graduated from gated to implemented; ATR backend and CW/FT8/recording pages remain the only outstanding gated hooks.

## AD-010: Control TX Power via the Device DRIVE Command (0x0017)

| Attribute | Value |
|-----------|-------|
| Type | Architecture |
| Status | Implemented |
| Decision | Set TX output power with the device DRIVE command (`0x0017`), not software IQ amplitude. Drive % is runtime-configurable per band and persisted. |

**Problem**: Early TX produced very low output. Root cause: software scaled IQ amplitude for "power" while the drive byte was either never sent or placed in the wrong packet field (payload instead of the trailing word), so the device received drive=0.

**Rationale**: SunSDR2 firmware does not support ALC, so per-band drive is the only real power lever. The drive byte uses a square-root taper (`round(255·√(drive%/100))`) and must sit in the packet **trailing word** (verified byte-for-byte against an ExpertSDR3 TCI capture). The device resets drive to a per-band calibration value on every QSY, so it is re-sent on frequency change and before each PTT assert.

**Consequences**: Software IQ gain stays gentle (clean SSB envelope only); power is owned by the device. Per-band power is user-editable via `/api/band_power` (persisted to `band_power.json`) and the Band Power menu panel. On-air verified: Tune ~12 W, voice 30–40 W PEP. Device telemetry (`0x1F00`) is sent in all modes (verified: 273 TX-state packets); off16 u16/100 gives SWR (1.32-1.37 range).

## AD-011: Read TX Telemetry SWR from off16 u16/100

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Read SWR from `0x1F00` offset 16 as u16 ÷ 100, replacing the prior offset 22 (f32, only valid during TUNE) and offset 26 (f32, constant 1.0000). |

**Problem**: The original PROTOCOL.md documented `0x1F00` off26 as f32 SWR, but this field is always exactly 1.0000 across 323+ packets in 6 captures (RX, TX, TUNE modes all identical). Off22 f32 varies but reads 0.0 during normal voice TX — SWR is only populated during TUNE mode there. The real continuous SWR field is off16 u16: values 132-137 across captures → 1.32-1.37 SWR, varying between antenna states.

**Rationale**: Off16 u16/100 is the only field that varies in SWR-range (1.0-3.0) across all device modes and produces values consistent with the user's external SWR meter (1.1-1.3). Verified against 323+ packets across `sunsdr_sdr_tx.pcap`, `sunsdr_tx_full.pcap`, `telem_full.pcap`, and `tune_only.pcap`.

**Consequences**: Device reports SWR continuously (RX/TX/TUNE), unlike off22 which is TUNE-only. External ATR-1000 tuner remains the authoritative SWR source for precision measurement; device telemetry provides a approximate indication.

## 8.11 Decision Summary

| ID | Topic | Status |
|----|-------|--------|
| AD-001 | FastAPI/Uvicorn backend | Implemented |
| AD-002 | Direct SunSDR2 DX UDP control | Implemented |
| AD-003 | DSP-owned mode | Implemented |
| AD-004 | Tagged dual-codec audio transport (Opus + Int16 PCM, RX and TX) | Implemented |
| AD-005 | Quantized spectrum frames | Implemented |
| AD-006 | HTTPS/WSS by default | Implemented |
| AD-007 | PTT release safety flow | Implemented |
| AD-008 | Optional WDSP | Implemented |
| AD-009 | Explicit frontend/backend gap tracking | Active |
| AD-010 | TX power via device DRIVE (0x0017) | Implemented |
| AD-011 | SWR from 0x1F00 off16 u16/100 | Implemented |

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

## AD-004: Use Int16 PCM as Current RX Transport

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | Broadcast RX audio as 16 kHz Int16 PCM over `/WSaudioRX` |

**Problem**: Browser RX must be reliable across iOS and desktop during active debugging.

**Rationale**: Int16 PCM removes Opus negotiation ambiguity and simplifies decode/playback.

**Consequences**: Bandwidth is higher than Opus, but behavior is transparent and easier to validate.

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

**Consequences**: Software IQ gain stays gentle (clean SSB envelope only); power is owned by the device. Per-band power is user-editable via `/api/band_power` (persisted to `band_power.json`) and the Band Power menu panel. On-air verified: Tune ~12 W, voice 30–40 W PEP. Note: the device stops sending `0x1F00` telemetry while keyed, so forward power must be read from the ATR-1000 tuner or a wattmeter, not the in-stream telemetry.

## 8.10 Decision Summary

| ID | Topic | Status |
|----|-------|--------|
| AD-001 | FastAPI/Uvicorn backend | Implemented |
| AD-002 | Direct SunSDR2 DX UDP control | Implemented |
| AD-003 | DSP-owned mode | Implemented |
| AD-004 | Int16 PCM RX transport | Implemented |
| AD-005 | Quantized spectrum frames | Implemented |
| AD-006 | HTTPS/WSS by default | Implemented |
| AD-007 | PTT release safety flow | Implemented |
| AD-008 | Optional WDSP | Implemented |
| AD-009 | Explicit frontend/backend gap tracking | Active |
| AD-010 | TX power via device DRIVE (0x0017) | Implemented |

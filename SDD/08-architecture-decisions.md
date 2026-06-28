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

**Consequences**: Software IQ gain stays gentle (clean SSB envelope only); power is owned by the device. Per-band power is user-editable via `/api/band_power` (persisted to `band_power.json`) and the Band Power menu panel. On-air verified: Tune ~12 W, voice 30–40 W PEP. Device telemetry (`0x1F00`) reports off30 f32 = forward watts, off16 u16/10 = supply volts, off18 f32 = PA temp °C (no reverse-power / SWR field — see AD-011).

## AD-011: Read TX Telemetry from 0x1F00 Verified Field Offsets

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented (corrected 2026-06-25) |
| Decision | Read `0x1F00` telemetry from verified field offsets reverse-engineered from a real 40m drive sweep (`device/captures/expert_40m_drive.pcap`, cross-checked against external wattmeter + ExpertSDR3's own power readout): off30 f32 = forward power (W PEP), off16 u16 = supply voltage ×10, off18 f32 = PA temperature °C. The device exposes NO reverse-power field → SWR cannot be computed from device telemetry. |

**Problem**: Prior SDD versions documented off16 u16/100 as SWR and off14 u16 as power_raw with a cubic fit. Both were wrong:
- **off30 f32 = forward power in WATTS** (PEP envelope). Monotonic with drive: 28%→3W, 71%→54W, 88%→83W, 100%→101W. Matches ExpertSDR3's ~95W self-readout at 100%. Direct float watts — no fit needed.
- **off16 u16 = supply voltage ×10** (NOT SWR). Reads ~136 (13.6V) at idle, sags as power rises (0W→13.6, 30-50W→13.1, 80-110W→12.9V); correlation with forward power = -0.79 — a textbook PSU sag curve. Volts = off16 / 10.
- **off18 f32 = PA temperature °C** (~42°C, barely moves with drive).
- **off22 f32** = average forward power (ratio to off30 ≈ the 3:1 SSB crest factor), not reverse power.
- **off26 f32** = always 1.0000 (device placeholder).
- The device sends NO reverse-power field, so it cannot compute SWR at all.

**Rationale**: Field offsets cross-validated against a complete 40m drive sweep (10 power levels, 100 packets each) with simultaneous external wattmeter readings and ExpertSDR3's own power readout. The monotonicity of off30 with drive and the PSU-sag pattern of off16 (correlation -0.79 with forward power) are textbook-confirming.

**Consequences**: Frontend `getTXTelem` carries `watts,volts,temp` (not SWR). The UI VOLT field replaces the former SWR field. The old off14-based cubic fit and off16/100 "SWR" computation are removed. The external ATR-1000 tuner is the only available SWR source; device telemetry provides forward power, supply voltage, and PA temperature only.

## AD-012: Client-to-Server TX Audio Gain Staging with Soft Limiter

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Stage TX audio gain across client and server so the server-side tanh soft limiter at `TX_IQ_PEAK` (1.0) engages lightly (~4% peak reduction) rather than serving as the primary gain-control element. Client preamp ×1.5 (+3.5dB) provides headroom; server `TX_DRIVE_GAIN` ×3.0 lifts the Hilbert-transformed SSB into the tanh knee. Device drive (`0x0017`) remains the sole RF power control. |

**Problem**: On 2026-06-25, voice TX was heavily distorted despite adequate power. Server log level probes (`TX chain in/drv/lim`) revealed the root cause: the client preamp was ×3.0 (+9.5dB), which drove the AudioWorklet Int16 output to full scale (peak=1.0). After Hilbert SSB (+~30% peak from analytic-phase construction) and server drive gain (×3.0), peaks reached 3.95 — the tanh limiter at 1.0 had to squash 75% of peak amplitude, producing heavy saturation distortion on every loud syllable.

Tune mode was unaffected because it bypasses the entire client audio chain and server gain path, using a pre-computed IQ signal with `TX_TUNE_SCALE` = 0.35.

**Rationale**: 
- **Client preamp 3.0→1.5** (-6dB): Gives the server input headroom. Peaks now arrive at ~0.5 (not 1.0). After Hilbert → ~0.65, drive ×3.0 → ~1.95, tanh(1.95) → ~0.96 — only a ~4% peak reduction.
- **Server `TX_DRIVE_GAIN` = 3.0**: Fixed make-up gain to compensate for phone mic's naturally low level (~0.1-0.3 peak). Not a power control — device drive is the power lever.
- **Tanh at ceiling 1.0**: Smooth saturation rounds transients without the wideband splatter of a hard IQ-magnitude clip (see CLAUDE.md). Acts as a safety net for residual transients, not the primary limiter.
- **Device drive (`0x0017`) sets actual RF power**: Square-root taper byte in trailing word, per-band configurable via `/api/band_power`, re-sent on QSY and PTT.

**Level-probe diagnostics** (logged at 1 Hz in `server.log` during TX):
```
TX chain in : rms, peak — post-decode PCM (before Hilbert, scaled by 1/gain for comparison)
TX chain an : rms, peak — after Hilbert analytic signal (same gain normalization)
TX chain drv: rms, peak — after TX_DRIVE_GAIN × drive (pre-tanh, full scale)
TX chain lim: rms, peak — after tanh(TX_IQ_PEAK=1.0), final IQ envelope
```
Healthy gain staging shows `in` peak ~0.5, `drv` peak ~2.0, `lim` peak ~0.96. If `in` peak = 1.0 and `drv` peak > 3.5, the tanh is oversaturated — voice will sound distorted.

**Consequences**: Client gain staging is now documented as a design decision, not an implementation detail. The level probes provide continuous observability. TX power adjustment is done via device drive %, not by changing TX_DRIVE_GAIN (which would upset the gain staging).

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
| AD-011 | TX telemetry from 0x1F00 verified field offsets (forward W, supply V, PA temp °C; no SWR) | Implemented (corrected 2026-06-25) |
| AD-012 | TX audio gain staging: client preamp ×1.5, server TX_DRIVE_GAIN ×3.0, TX_IQ_PEAK 1.0, tanh soft limiter with light engagement | Implemented |

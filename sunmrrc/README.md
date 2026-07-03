# sunmrrc — SunSDR2 DX Web Control Server

FastAPI-based HTTPS/WSS server for the SunSDR2 DX SDR transceiver. Provides web-based control from any modern browser (optimized for iPhone/iOS Safari).

## Directory structure

```
sunmrrc/
├── server.py           # FastAPI app, WebSocket endpoints, IQ processing loop
├── restart.sh          # Start/stop/restart script
├── static/             # Frontend assets
│   ├── index.html      # Desktop UI
│   ├── controls.js     # WebSocket setup, RX/TX audio, waterfall, FFT
│   ├── mobile.js       # Mobile UX, band/mode buttons, DSP panel
│   ├── mobile.css      # Mobile layout and styling
│   ├── modules/        # PTT manager, settings, TX audio EQ, Opus codec
│   └── recordings/     # Saved audio recordings (created at runtime)
├── certs/              # TLS certificates (gitignored)
│   ├── fullchain.pem
│   └── radio.vlsc.net.key
├── captures/           # TX diagnostic WAV/CSV files (created at runtime)
├── band_power.json     # Per-band TX power settings (created at runtime)
└── mem_channels.json   # Memory channel storage (created at runtime)
```

## Quick start

```bash
./restart.sh            # background, default port 8889
./restart.sh -f         # foreground with live logs
WEB_PORT=8080 ./restart.sh   # custom port
```

Open `https://localhost:8889` (or your domain matching the TLS cert).

Logs: `tail -f sunmrrc/server.log`

## Key features

- 5 WebSocket endpoints: `/WSCTRX`, `/WSaudioRX`, `/WSaudioTX`, `/WSspectrum`, `/WSATR1000`
- Real-time waterfall (512-bin FFT, 38 Hz) with adaptive noise floor
- RX audio: SSB/AM/FM/CW, Opus (default) or Int16 PCM
- TX audio: browser mic → SAB ring buffer → Opus → Python Hilbert SSB → 24-bit IQ
- WDSP noise reduction (NR2, NB, ANF, AGC) — optional, runtime-toggled
- Per-band TX power control via `/api/band_power`
- Memory channels (6 slots) via `/api/mem_channels`
- Audio recording via `startRecording`/`stopRecording` commands
- TX telemetry (power, voltage, temp) from 0x1F00 packets

## Authentication

All routes require a session token. Default password: `sunmrrc` (override with `WEB_PASSWORD` env var).

- Login page → `POST /api/auth/login` → sets `sunmrrc_auth` cookie (30-day lifetime)
- WebSocket auth: `?token=` query parameter (required because browser WebSocket API doesn't support custom headers)

See main `../README.md` for full documentation.

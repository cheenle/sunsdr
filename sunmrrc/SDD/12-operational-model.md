# 12. Operational Model (ART 0522)

## 12.1 Runtime Topology

```text
Client Browser
  -> https://radio.vlsc.net:8889
  -> WSS endpoints on same host/port

SunMRRC Host
  -> python3 server.py under ../venv when available
  -> Uvicorn on 0.0.0.0:$WEB_PORT
  -> UDP bind 192.168.16.100:50001 for control client
  -> UDP bind 192.168.16.100:50002 for IQ loop
  -> logs to server.log when started by restart.sh

SunSDR2 DX
  -> default DEVICE_HOST 192.168.16.200
  -> control UDP :50001
  -> stream UDP :50002
```

## 12.2 Configuration

| Name | Default | Purpose |
|------|---------|---------|
| `DEVICE_HOST` | `192.168.16.200` | SunSDR2 DX control host for `SunSDR2DXClient` |
| `WEB_PORT` | `8889` in `restart.sh` and `start.sh`, `8889` in `server.py` default | Uvicorn listen port |
| `DISABLE_SSL` | unset | Set to `1` to force HTTP even when certs exist |
| `BACKEND` | `direct` in scripts | Operational marker; current `server.py` uses direct client path |
| `NO_PROXY` | `127.0.0.1,localhost` in `restart.sh` | Avoid local proxy interference |

## 12.3 Startup Modes

| Mode | Command | Behavior |
|------|---------|----------|
| Simple start | `./start.sh` | Activates `../venv` if present, defaults `WEB_PORT=8889`, execs `python3 server.py` |
| Background restart | `./restart.sh` | Defaults `WEB_PORT=8889`, kills old cwd-matched server, clears listen port, writes `server.log` |
| Foreground restart | `./restart.sh -f` | Same cleanup, then runs foreground for live logs |
| HTTP debug | `DISABLE_SSL=1 ./restart.sh` | Forces HTTP; not suitable for iOS mic/audio validation |

## 12.4 TLS Operation

| Item | Path/Behavior |
|------|---------------|
| Certificate | `certs/fullchain.pem` |
| Private key | `certs/radio.vlsc.net.key` |
| Auto-detection | `_find_ssl()` returns cert/key when both files exist |
| HTTPS log | `sunmrrc https://0.0.0.0:<port>` and `TLS: <cert>` |
| HTTP fallback log | Warning that iOS may have no sound/microphone |
| Expiry support | `certs/check_ssl_expiry.sh`, `certs/expiry_check.log` |

## 12.5 Connection Matrix

| Source | Target | Protocol | Port/Path | Description |
|--------|--------|----------|-----------|-------------|
| Browser | SunMRRC | HTTPS | `$WEB_PORT` | Static UI |
| Browser | SunMRRC | WSS/WS | `/WSCTRX` | Control |
| Browser | SunMRRC | WSS/WS | `/WSaudioRX` | RX audio |
| Browser | SunMRRC | WSS/WS | `/WSaudioTX` | TX audio ingress placeholder |
| Browser | SunMRRC | WSS/WS | `/WSspectrum` | Waterfall |
| SunMRRC | SunSDR2 DX | UDP | `:50001` | Control protocol |
| SunSDR2 DX | SunMRRC | UDP | local `:50002` | IQ stream |

## 12.6 Operational Procedures

| Procedure | Steps |
|-----------|-------|
| Verify HTTPS startup | Run `./restart.sh`; inspect `server.log` for HTTPS and TLS lines |
| Verify radio connect | Inspect `server.log` for `SunSDR2DX: True` and `IQ: port 50002` |
| Verify mobile entry | Open `https://radio.vlsc.net:8889` from iPhone with correct DNS/LAN routing |
| Verify RX | Power on UI, confirm `/WSaudioRX` connected and audio/bitrate active |
| Verify control | Change frequency/mode and confirm UI ack plus radio behavior |
| Verify PTT safety | Press/release PTT; confirm `getPTT:false` after release |

## 12.7 Logs and Artifacts

| Artifact | Purpose |
|----------|---------|
| `server.log` | Runtime logs when background-started |
| `STATUS.md` | Current debugging/status notes |
| `certs/backup/` | Timestamped certificate backups |
| `certs/expiry_check.log` | Certificate expiry check output |

## 12.8 Operational Risks

| Risk | Mitigation |
|------|------------|
| Wrong port default between scripts | Resolved — all scripts now default to `WEB_PORT=8889` |
| iOS loaded over HTTP | Use HTTPS domain entry, not raw HTTP IP |
| Fixed local UDP bind IP does not match host | Update `server.py` or host network before deployment |
| Old JS cached | `sw.js` bypasses JS/HTML; version query strings also used on scripts |
| Stuck TX | PTT release ACK retry, watchdog, backend `s` command |

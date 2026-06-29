# 15. PTT Safety Architecture (ART 0535)

> **Principle**: Release is more safety-critical than keying. A lost `setPTT:false` can leave a radio transmitting indefinitely. Every layer must have an independent path to force RX.

## 15.1 Safety Model — Defense in Depth

```
Layer 1: Touch UX        touch-end / touch-move-out / touch-cancel → TXControl('stop')
Layer 2: State Machine    isProcessing lock (3s leak detection) + pendingStop queue
Layer 3: Watchdog         30s hardware timer → force release regardless of state
Layer 4: Command ACK      setPTT:false → wait for getPTT:false echo → retry 3×
Layer 5: Backup Channel   TX audio WS sends "s:" → independent socket → forced-RX
Layer 6: PING Health      periodic PING/PONG → stale connection detected → reconnect
Layer 7: Page Lifecycle   visibilitychange / blur / pagehide → force release
Layer 8: Server Forced-RX ws_ctrl() s: handler → radio.set_ptt(False) — always served
```

**No single point of failure can trap the radio in TX.**

## 15.2 Layer 1 — Touch UX (tx_button.js)

| Event | Action | Why |
|-------|--------|-----|
| `touchstart` | `TXControl('start')` | Press begins TX |
| `touchend` | `TXControl('stop')` | Normal release |
| `touchmove` out of button | `TXControl('stop')` | Finger slides off → safety release |
| `touchcancel` | `TXControl('stop')` | OS interrupts touch → safety release |
| `mouseleave` (desktop) | `TXControl('stop')` | Cursor leaves button → safety release |

Each event handler uses `touchId` tracking to prevent cross-touch interference. `{ passive: false, capture: true }` ensures the handlers fire before any other listener on the page and can call `preventDefault()`.

## 15.3 Layer 2 — State Machine Lock (tx_button.js)

```
TXState.isProcessing  ──  prevents concurrent start/stop races
TXState.pendingStop    ──  queue stop request when start is in-flight
PROCESSING_LOCK_TIMEOUT_MS = 3000  ──  leak detection: force-unlock stuck lock
```

**Lock lifecycle**:
- `start`: sets `isProcessing=true`, records `processingStartTime`, clears `pendingStop`. On completion, checks `pendingStop` — if set, immediately calls `TXControl('stop')`.
- `stop`: if `isProcessing`, sets `pendingStop=true` and returns (queued). Otherwise acquires lock, executes release, unlocks.
- **Leak detection**: if `isProcessing` held >3s, force-cleared. Prevents a single uncaught exception from permanently blocking all stop calls.

## 15.4 Layer 3 — Hardware Watchdog (tx_button.js)

```
30-second timeout started on every TX start.
  → clears if TXControl('stop') runs normally
  → fires: force-release PTT, clear audio, reset UI
```

The watchdog is the ultimate frontend failsafe. Even if every other layer fails (JS error, dead WebSocket, frozen event loop), within 30 seconds the watchdog timer fires and calls `sendTRXptt(false)` + full state cleanup.

## 15.5 Layer 4 — Command ACK Retry (ptt_manager.js)

```
setPTT:false sent
  → start 1s ACK timer
    → getPTT:false received (device confirmed RX) → timer cancelled ✓
    → 1s elapsed, no confirmation:
      → retry 1: re-send setPTT:false + backup s: on TX WS
      → 1s elapsed, no confirmation:
        → retry 2: re-send setPTT:false + backup s: on TX WS
      → 1s elapsed, no confirmation:
        → retry 3: re-send setPTT:false + backup s: on TX WS
      → 1s elapsed, no confirmation:
        → onControlConnectionDead() — force-reconnect control channel
```

**Design rationale**: `setPTT:true` (key) has no ACK — a lost key command means no TX, which is safe. `setPTT:false` (release) is the dangerous one: a lost release means stuck TX. The ACK retry loop ensures confirmation, using the TX audio socket as an independent backup channel on every retry.

## 15.6 Layer 5 — Backup Channel (s: command)

```
wsAudioTX.send("s:")  →  server ws_audio_tx() handler
  →  dsp_proc.modulator.reset_mic()
  →  radio.set_ptt(False)
  →  dsp_proc.demodulator.set_ptt(False)
  →  _send_ctrl("getPTT:false")  ←  broadcasts to ALL control clients
```

The `s:` command on the TX audio WebSocket is an independent path to force RX. It does not depend on the control WebSocket (`/WSCTRX`) being alive. The server-side handler in `ws_audio_tx()` directly calls `radio.set_ptt(False)` and broadcasts the state change.

**Why this works**: The TX audio socket and control socket are separate TCP connections. WiFi/router glitches that half-close one rarely affect the other simultaneously. This is the critical redundancy that breaks the single-point-of-failure.

## 15.7 Layer 6 — PING/PONG Health Check (controls.js)

```
Every 5s: wsControlTRX.send("PING")
  → start PONG_TIMEOUT_MS timer
  → PONG received → round-trip latency display ✓
  → timer expired → onControlConnectionDead()
    → send s: on TX audio socket (if TX is pressed)
    → force-close and reconnect control socket
```

TCP half-open connections (where the server thinks the socket is alive but the client's WiFi has dropped) are the most insidious failure mode. PING/PONG detection catches these within one timeout cycle, triggers backup release before reconnect.

## 15.8 Layer 7 — Page Lifecycle (tx_button.js)

```
document.addEventListener('visibilitychange', ...)
window.addEventListener('blur', ...)
window.addEventListener('pagehide', ...)
  → if TXState.isPressed → TXControl('stop')
```

When the browser tab is backgrounded, the OS may suspend timers and JavaScript execution. If the user switches away while TX is active, the touch-end event is never delivered. These lifecycle hooks catch that case and force release.

## 15.9 Layer 8 — Server-Side Forced RX (server.py)

```python
# ws_ctrl() handler — primary control path
elif cmd == "setPTT":
    tx = val.lower() == "true"
    await radio.set_ptt(tx)
    if dsp_proc: dsp_proc.demodulator.set_ptt(tx)
    await ws.send_text(f"getPTT:{str(tx).lower()}")

# ws_ctrl() handler — backup forced-RX
elif cmd == "s":
    await radio.set_ptt(False)
    if dsp_proc: dsp_proc.demodulator.set_ptt(False)
    await _send_ctrl("getPTT:false")
```

The server echoes every PTT state change as `getPTT:<bool>` over the control channel. The client uses this echo as delivery confirmation (Layer 4). The `s:` command is handled identically to `setPTT:false` — no difference in server behavior, just an alternative entry point.

On the device side, `radio.set_ptt(True)` re-sends the DRIVE byte (`0x0017`) before keying, and `radio.set_ptt(False)` sends the PTT command with `trailing=0` to release.

## 15.10 TX Pacer Thread Lifecycle

```
PTT asserted  →  TX pacer thread starts
  → TX_SETTLE_PACKETS (17) zero-IQ packets (PA/relay settling)
  → TX_RAMP_SAMPLES (200) linear ramp 0→1
  → Real mic IQ or tune IQ at 5.12ms/pkt
PTT released  →  TX pacer thread stops
```

The pacer thread is tied to `radio._ptt_active`. When PTT releases (from any path), the while-loop exits, the thread joins, and no further IQ packets are sent. This is the hardware-level guarantee that TX stops when PTT is false.

The TX modulation path is pure Python (Hilbert SSB in `dsp.py` `TXModulator`); the WDSP C-chain was removed and has no bearing on PTT safety. All safety layers operate above the DSP level.

## 15.11 PTT Status Display

| State | Color | Meaning |
|-------|-------|---------|
| `PTT: ON` | Green | Device confirmed TX active |
| `PTT: ON` | Yellow | Predicted (command sent, not yet confirmed) |
| `PTT: OFF` | Red | Device confirmed RX |
| `PTT: OFF` | Orange | Predicted release (not yet confirmed) |
| `PTT: ?` | Gray blinking | State unknown (prediction expired) |

The display always prefers device-confirmed state over prediction. A background sync loop (1 Hz) queries `getPTT` if device state disagrees with user intent.

## 15.12 Failure Mode Coverage

| Failure | Layer | Mitigation |
|---------|-------|------------|
| Finger slips off button | 1 | `touchmove` out-of-bounds → stop |
| OS steals touch (notification) | 1 | `touchcancel` → stop |
| JS exception in start() blocks stop() | 2 | `pendingStop` queue + lock leak detection |
| Event loop frozen / timer starvation | 3 | 30s hardware watchdog (setTimeout-based) |
| setPTT:false lost (WiFi packet loss) | 4 | 3× ACK retry with getPTT:false confirmation |
| Control WS half-open (TCP stale) | 5,6 | Backup s: on TX WS + PING/PONG detection |
| Control WS fully dead | 5 | TX WS s: command → server forced-RX |
| User switches app (iOS suspend) | 7 | `visibilitychange` + `blur` → force release |
| All frontend paths fail | 8 | Server `s:` handler — always available |

## 15.13 Component Mapping

| Component | File | Role |
|-----------|------|------|
| TXButton | `tx_button.js` | Touch UX, state machine, lock, watchdog, lifecycle hooks |
| PTTManager | `ptt_manager.js` | ACK retry, release confirmation, status display, debounce |
| ControlWS | `server.py` ws_ctrl() | Primary setPTT dispatch, getPTT echo, s: forced-RX |
| TXAudioWS | `server.py` ws_audio_tx() | Backup s: path — independent TCP connection |
| RadioClient | `sunsdr_direct.py` set_ptt() | DRIVE re-send (TX assert), PTT command, _ptt_active flag |
| TXPacer | `server.py` _tx_pacer_thread() | Thread lifecycle tied to _ptt_active → stops on release |
| HealthCheck | `controls.js` checklatency() | PING/PONG dead-connection detection + reconnect |

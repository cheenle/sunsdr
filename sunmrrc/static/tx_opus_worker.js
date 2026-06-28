// TX Opus Worker — reads float32 samples from SharedArrayBuffer ring,
// encodes with Opus, and sends via its own WebSocket.
//
// Zero main-thread involvement after setup:
//   AudioWorklet (audio thread) → SAB ring buffer → Opus Worker (this thread)
//
// The main thread only creates the SAB and passes it to both sides — it
// never touches audio samples, so GC pauses / UI jank can't stall TX.
var AUDIO_TAG_OPUS = 0x01;
var AUDIO_TAG_PCM  = 0x00;
var FRAME_SIZE = 320;   // 20 ms @ 16 kHz
var POLL_MS = 3;        // check SAB every 3 ms (audio thread quanta ≈ 2.67 ms)

function _queryToken() {
  var m = String(self.location.search || '').match(/[?&]token=([^&]+)/);
  return m ? decodeURIComponent(m[1].replace(/\+/g, ' ')) : '';
}

var AUTH_TOKEN = _queryToken();
function withToken(path) {
  return path + (AUTH_TOKEN ? (path.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(AUTH_TOKEN) : '');
}

importScripts(withToken('/modules/opus_wasm.js'), withToken('/modules/opus_codec.js'));

// ── SAB ring buffer (consumer side) ────────────────────
// Layout: word[0]=write_pos, word[1]=read_pos, word[2+]=float32 data

var _sab = null;
var _ringMask = 0;
var _writePtr = null;
var _readPtr = null;
var _data = null;

function sabInit(sab, ringSize) {
  _sab = sab;
  _ringMask = ringSize - 1;
  _writePtr = new Uint32Array(sab, 0, 1);
  _readPtr  = new Uint32Array(sab, 4, 1);
  _data     = new Float32Array(sab, 8, ringSize);
}

function sabAvailable() {
  if (!_sab) return 0;
  return Atomics.load(_writePtr) - Atomics.load(_readPtr);
}

function sabRead(n) {
  if (!_sab) return null;
  var wp = Atomics.load(_writePtr);
  var rp = Atomics.load(_readPtr);
  var avail = wp - rp;
  n = Math.min(n, avail);
  if (n <= 0) return new Float32Array(0);
  var out = new Float32Array(n);
  var idx = rp & _ringMask;
  var size = _ringMask + 1;
  var first = Math.min(n, size - idx);
  for (var i = 0; i < first; i++) out[i] = _data[idx + i];
  if (n > first) {
    for (var i = 0; i < n - first; i++) out[first + i] = _data[i];
  }
  Atomics.store(_readPtr, rp + n);
  return out;
}

// ── Opus encoder ───────────────────────────────────────

var encoder = null;
var ws = null;
var wsConnecting = false;
var running = false;
var _pollTimer = null;
var _useOpus = true;      // true = Opus encode, false = raw Int16 PCM
var _pcmAcc = null;       // accumulate for PCM path (rarely used)
var dropped = 0;

function wsUrl() {
  var proto = self.location.protocol === 'https:' ? 'wss://' : 'ws://';
  return proto + self.location.host + '/WSaudioTX' +
    (AUTH_TOKEN ? '?token=' + encodeURIComponent(AUTH_TOKEN) : '');
}

function ensureEncoder() {
  if (!encoder) {
    encoder = new OpusEncoder(16000, 1, 2048, 20);
  }
}

function ensureWs() {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  if (wsConnecting) return;
  wsConnecting = true;
  ws = new WebSocket(wsUrl());
  ws.binaryType = 'arraybuffer';
  ws.onopen = function() {
    wsConnecting = false;
    try { ws.send('m:16000,1,16000,20'); } catch (e) {}
    self.postMessage({ type: 'open' });
  };
  ws.onerror = function() {
    self.postMessage({ type: 'error', message: 'TX Opus worker websocket error' });
  };
  ws.onclose = function(e) {
    wsConnecting = false;
    ws = null;
    if (e && e.code === 4001) {
      running = false;
      self.postMessage({ type: 'authExpired' });
      return;
    }
    if (running) {
      setTimeout(ensureWs, 250);
    }
    self.postMessage({ type: 'closed' });
  };
}

function sendPacket(packet) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    ensureWs();
    return false;
  }
  var opusBytes = new Uint8Array(packet);
  var tagged = new Uint8Array(1 + opusBytes.length);
  tagged[0] = AUDIO_TAG_OPUS;
  tagged.set(opusBytes, 1);
  ws.send(tagged);
  return true;
}

function sendPcmFrame(pcmInt16) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    ensureWs();
    return false;
  }
  var tagged = new Uint8Array(1 + pcmInt16.byteLength);
  tagged[0] = AUDIO_TAG_PCM;
  tagged.set(new Uint8Array(pcmInt16.buffer, pcmInt16.byteOffset, pcmInt16.byteLength), 1);
  ws.send(tagged);
  return true;
}

function encodeAndSend(floatSamples) {
  if (_useOpus) {
    ensureEncoder();
    var packets = encoder.encode_float(floatSamples);
    for (var p = 0; p < packets.length; p++) {
      sendPacket(packets[p]);
    }
  } else {
    // PCM fallback: convert float32 → Int16 and send directly
    var i16 = new Int16Array(floatSamples.length);
    for (var i = 0; i < floatSamples.length; i++) {
      var v = floatSamples[i] * 32767;
      i16[i] = v > 32767 ? 32767 : (v < -32768 ? -32768 : (v | 0));
    }
    sendPcmFrame(i16);
  }
}

// ── Poll loop: drain SAB every POLL_MS ─────────────────

function pollSAB() {
  if (!running) return;
  ensureWs();
  var avail = sabAvailable();
  while (avail >= FRAME_SIZE) {
    var frame = sabRead(FRAME_SIZE);
    if (!frame || frame.length < FRAME_SIZE) break;
    encodeAndSend(frame);
    avail = sabAvailable();
  }
}

function startPolling() {
  if (_pollTimer) return;
  _pollTimer = setInterval(pollSAB, POLL_MS);
}

function stopPolling() {
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
}

// ── Main-thread commands ───────────────────────────────

self.onmessage = function(ev) {
  var d = ev.data || {};
  if (d.type === 'sab') {
    // Receive SharedArrayBuffer ring from main thread
    sabInit(d.sab, d.ringSize || 16384);
    self.postMessage({ type: 'sab_ready' });
    // If we're already running (start arrived first), begin polling now
    if (running && _sab) startPolling();
  } else if (d.type === 'config') {
    if (typeof d.useOpus === 'boolean') _useOpus = d.useOpus;
  } else if (d.type === 'frame') {
    // Legacy postMessage path (SAB unavailable)
    if (!running || !d.frame) return;
    var frame = new Int16Array(d.frame);
    var f32 = new Float32Array(frame.length);
    for (var i = 0; i < frame.length; i++) f32[i] = frame[i] / 32768.0;
    encodeAndSend(f32);
  } else if (d.type === 'start') {
    running = true;
    dropped = 0;
    ensureEncoder();
    ensureWs();
    // Only start SAB polling if SAB is available; legacy path relies on 'frame' messages
    if (_sab) startPolling();
  } else if (d.type === 'stop') {
    running = false;
    stopPolling();
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send('s:'); } catch (e) {}
    }
  } else if (d.type === 'close') {
    running = false;
    stopPolling();
    if (ws) {
      try { ws.send('s:'); } catch (e) {}
      try { ws.close(); } catch (e) {}
    }
    ws = null;
  }
};

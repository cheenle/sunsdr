// TX Opus worker: owns Opus encoding and the /WSaudioTX WebSocket. It does not
// try to be the timing authority; the server-side TX uplink pacer smooths any
// browser/worker burst before the modulator.
var AUDIO_TAG_OPUS = 0x01;
var TX_OPUS_WORKER_MAX_QUEUE = 16;

function _queryToken() {
    var m = String(self.location.search || '').match(/[?&]token=([^&]+)/);
    return m ? decodeURIComponent(m[1].replace(/\+/g, ' ')) : '';
}

var AUTH_TOKEN = _queryToken();
function withToken(path) {
    return path + (AUTH_TOKEN ? (path.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(AUTH_TOKEN) : '');
}

importScripts(withToken('/modules/opus_wasm.js'), withToken('/modules/opus_codec.js'));

var encoder = null;
var ws = null;
var wsConnecting = false;
var running = false;
var queue = [];
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
    if (ws && ws.readyState === WebSocket.OPEN) {
        return;
    }
    if (wsConnecting) {
        return;
    }
    wsConnecting = true;
    ws = new WebSocket(wsUrl());
    ws.binaryType = 'arraybuffer';
    ws.onopen = function() {
        wsConnecting = false;
        try { ws.send('m:16000,1,16000,20'); } catch (e) {}
        drainQueue();
        self.postMessage({ type: 'open' });
    };
    ws.onerror = function(e) {
        self.postMessage({ type: 'error', message: 'TX Opus worker websocket error' });
    };
    ws.onclose = function(e) {
        wsConnecting = false;
        ws = null;
        if (e && e.code === 4001) {
            running = false;
            queue = [];
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
    self.postMessage({ type: 'sent', bytes: tagged.byteLength, dropped: dropped });
    return true;
}

function encodeAndSend(int16Frame) {
    ensureEncoder();
    var f32 = new Float32Array(int16Frame.length);
    for (var i = 0; i < int16Frame.length; i++) {
        f32[i] = int16Frame[i] / 32768.0;
    }
    var packets = encoder.encode_float(f32);
    for (var p = 0; p < packets.length; p++) {
        if (!sendPacket(packets[p])) {
            queue.unshift(int16Frame);
            return false;
        }
    }
    return true;
}

function drainQueue() {
    if (!running) {
        queue = [];
        return;
    }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        ensureWs();
        return;
    }
    while (queue.length > 0 && ws && ws.readyState === WebSocket.OPEN) {
        var frame = queue.shift();
        if (frame) {
            encodeAndSend(frame);
        }
    }
}

self.onmessage = function(ev) {
    var d = ev.data || {};
    if (d.type === 'start') {
        running = true;
        dropped = 0;
        queue = [];
        ensureEncoder();
        ensureWs();
        drainQueue();
    } else if (d.type === 'frame') {
        if (!running || !d.frame) {
            return;
        }
        var frame = new Int16Array(d.frame);
        if (queue.length >= TX_OPUS_WORKER_MAX_QUEUE) {
            queue.shift();
            dropped += 1;
        }
        queue.push(frame);
        ensureWs();
        drainQueue();
    } else if (d.type === 'stop') {
        running = false;
        queue = [];
        if (ws && ws.readyState === WebSocket.OPEN) {
            try { ws.send('s:'); } catch (e) {}
        }
    } else if (d.type === 'close') {
        running = false;
        queue = [];
        if (ws) {
            try { ws.send('s:'); } catch (e) {}
            try { ws.close(); } catch (e) {}
        }
        ws = null;
    }
};

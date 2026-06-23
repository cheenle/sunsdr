// AudioWorklet TX capture: runs on a dedicated audio thread (not the main
// thread), so iOS Safari cannot stall the microphone stream when the page is
// in the background or the main thread is busy. The previous ScriptProcessor
// ran on the main thread and produced bursty, sometimes-stopped frames under
// iOS 14.5+; this is what caused the "mid-call dropouts" the far end heard.
//
// Pipeline (all inside the audio thread):
//   1. Collect 128-sample process() quanta @ 48 kHz into a ring buffer.
//   2. When the buffer reaches 1536 samples (32 ms), box-average downsample
//      3:1 → 512 samples @ 16 kHz.
//   3. Accumulate into a 320-sample (20 ms) frame accumulator; every time a
//      full frame is ready, post it back to the main thread as Int16 PCM.
//
// The main thread just forwards each Int16Array over the /WSaudioTX socket —
// it no longer does downsampling, accumulation, or format conversion. This
// keeps the main-thread footprint tiny so the audio thread is never blocked.

class TxCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // 48 kHz input, 16 kHz target, 3:1 decimation.
    this._inRate = (typeof sampleRate === 'number') ? sampleRate : 48000;
    this._outRate = 16000;
    // Decimation factor; if the audio context runs at 44.1 kHz we fall back
    // to integer 3 anyway (≈14.7 kHz effective output rate). The server's
    // fractional resampler handles the small mismatch.
    this._dsRatio = Math.max(1, Math.round(this._inRate / this._outRate));
    // Accumulate this many input samples before downsampling (keeps the
    // box-average well-aligned and amortizes the postMessage overhead).
    this._decimBlock = this._dsRatio * 160;     // 480 samples @ 48k ≈ 10 ms
    this._inBuf = new Float32Array(0);
    // 20 ms output frame size @ 16 kHz.
    this._frameSize = Math.round(this._outRate * 0.020);  // 320
    this._frameAcc = new Float32Array(0);
    // Gain applied before Int16 conversion (matches the ScriptProcessor's
    // 0x7FFF scale factor so the server sees the same amplitude).
    this._scale = 0x7FFF;

    this.port.onmessage = (ev) => {
      const d = ev.data || {};
      if (d.type === 'flush') {
        this._inBuf = new Float32Array(0);
        this._frameAcc = new Float32Array(0);
      } else if (d.type === 'config') {
        if (typeof d.scale === 'number' && d.scale > 0) {
          this._scale = d.scale;
        }
      }
    };
  }

  process(inputs, outputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) {
      return true;
    }
    const ch = input[0];

    // Append new samples to the input buffer.
    const merged = new Float32Array(this._inBuf.length + ch.length);
    merged.set(this._inBuf);
    merged.set(ch, this._inBuf.length);
    this._inBuf = merged;

    // Process input in fixed-size decimation blocks.
    const R = this._dsRatio;
    while (this._inBuf.length >= this._decimBlock) {
      const blockOut = this._decimBlock / R;
      const decimated = new Float32Array(blockOut);
      for (let i = 0; i < blockOut; i++) {
        let sum = 0;
        const base = i * R;
        for (let j = 0; j < R; j++) {
          sum += this._inBuf[base + j];
        }
        decimated[i] = sum / R;
      }
      this._inBuf = this._inBuf.subarray(this._decimBlock);

      // Append decimated samples to the frame accumulator and emit full frames.
      const accMerged = new Float32Array(this._frameAcc.length + decimated.length);
      accMerged.set(this._frameAcc);
      accMerged.set(decimated, this._frameAcc.length);
      this._frameAcc = accMerged;

      while (this._frameAcc.length >= this._frameSize) {
        const frame = this._frameAcc.subarray(0, this._frameSize);
        this._frameAcc = this._frameAcc.subarray(this._frameSize);
        // Convert to Int16 and post to main thread (transfer ownership if
        // possible, but Int16Array can't be neutered across the boundary in
        // all browsers — posting a copy is cheap at ~640 bytes).
        const i16 = new Int16Array(this._frameSize);
        for (let k = 0; k < this._frameSize; k++) {
          let v = frame[k] * this._scale;
          if (v > 32767) v = 32767;
          else if (v < -32768) v = -32768;
          i16[k] = v;
        }
        this.port.postMessage(i16);
      }
    }

    return true;
  }
}

registerProcessor('tx-capture', TxCaptureProcessor);

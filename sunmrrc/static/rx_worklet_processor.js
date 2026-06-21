// AudioWorklet RX player for raw Float32 frames pushed by controls.js.

class RxPlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.targetMinFrames = 2;
    this.targetMaxFrames = 30;
    this.underruns = 0;

    this.port.onmessage = (event) => {
      const data = event.data;
      if (data && data.type === 'push' && data.payload instanceof Float32Array) {
        this.queue.push(data.payload);
        while (this.queue.length > this.targetMaxFrames) {
          this.queue.shift();
        }
      } else if (data && (data.type === 'flush' || data.type === 'reset')) {
        this.queue.length = 0;
        this.underruns = 0;
      } else if (data && data.type === 'config') {
        if (typeof data.min === 'number') {
          this.targetMinFrames = Math.max(1, data.min | 0);
        }
        if (typeof data.max === 'number') {
          this.targetMaxFrames = Math.max(this.targetMinFrames + 1, data.max | 0);
        }
      }
    };
  }

  process(inputs, outputs) {
    const output = outputs[0];
    const out = output[0];

    if (!out) {
      return true;
    }

    if (this.queue.length === 0 || (this.targetMinFrames > 1 && this.queue.length < this.targetMinFrames)) {
      out.fill(0);
      this.underruns++;
      return true;
    }

    let written = 0;
    while (written < out.length && this.queue.length > 0) {
      const cur = this.queue[0];
      const n = Math.min(cur.length, out.length - written);
      out.set(cur.subarray(0, n), written);
      written += n;

      if (n >= cur.length) {
        this.queue.shift();
      } else {
        this.queue[0] = cur.subarray(n);
      }
    }

    if (written < out.length) {
      out.fill(0, written);
    }

    return true;
  }
}

registerProcessor('rx-player', RxPlayerProcessor);

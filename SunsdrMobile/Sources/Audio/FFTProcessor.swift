import Foundation

/// Processes raw 512-byte spectrum data into EMA-smoothed, noise-floor-normalized
/// values for the real-time FFT line plot. Runs entirely off the main thread.
/// Matches the web frontend's `_fftDraw()` in controls.js (commit a748bc2).
final class FFTProcessor: @unchecked Sendable {
    /// Number of spectrum bins from the server.
    private let binCount = 512

    /// EMA smoothing factor (alpha=0.25 → ~4-frame time constant).
    private let emaAlpha: Float = 0.25

    /// Percentile for adaptive noise-floor estimation (15%).
    private let floorPercentile: Float = 0.15

    /// Fixed dynamic range in raw uint8 units (~33 dB).
    private let dynRange: Float = 70.0

    /// Gamma exponent for visual dynamics (0.65 = mild).
    private let gamma: Float = 0.65

    /// Persistent EMA buffer — one entry per bin, values in raw uint8 space.
    private var emaBuf: [Float] = []

    /// Output buffer published to main thread after smoothing + normalization.
    /// Values are 0..1 after noise-floor subtraction and gamma.
    private(set) var smoothedData: [Float] = []

    /// Serial background queue — matches SpectrumProcessor pattern.
    private let queue = DispatchQueue(label: "fft.processor", qos: .userInteractive)

    // MARK: - Public API

    /// Feed raw 512-byte spectrum frame (uint8, 0=-120dB, 255=0dB).
    /// Callback delivers the smoothed [Float] (0..1) on the main thread.
    func feed(data: Data, onFrame: @escaping ([Float]) -> Void) {
        guard data.count >= binCount else { return }

        // Copy raw bytes to float on the calling thread (fast, no allocation).
        let raw = data.prefix(binCount).map { Float($0) }

        queue.async { [weak self] in
            guard let self = self else { return }
            let smoothed = self.process(raw: raw)
            DispatchQueue.main.async {
                onFrame(smoothed)
            }
        }
    }

    /// Reset EMA buffer (call after sample rate change / reconnect).
    func reset() {
        queue.async { [weak self] in
            self?.emaBuf.removeAll()
        }
    }

    // MARK: - Processing

    private func process(raw: [Float]) -> [Float] {
        // ── EMA smoothing ──────────────────────────────────────
        if emaBuf.count != raw.count {
            emaBuf = raw
        }
        var blended = [Float](repeating: 0, count: raw.count)
        for i in 0..<raw.count {
            emaBuf[i] = emaBuf[i] * (1.0 - emaAlpha) + raw[i] * emaAlpha
            blended[i] = emaBuf[i]
        }

        // ── 15th-percentile noise floor ────────────────────────
        let sorted = blended.sorted()
        let floorIdx = Int(Float(sorted.count) * floorPercentile)
        let floor = sorted[floorIdx]

        // ── Noise-floor subtraction + gamma ────────────────────
        var result = [Float](repeating: 0, count: blended.count)
        for i in 0..<blended.count {
            let rawVal = blended[i]
            if rawVal <= floor {
                result[i] = 0
            } else {
                var v = (rawVal - floor) / dynRange
                if v < 0 { v = 0 }
                if v > 1.0 { v = 1.0 }
                result[i] = pow(v, gamma)
            }
        }

        smoothedData = result
        return result
    }
}

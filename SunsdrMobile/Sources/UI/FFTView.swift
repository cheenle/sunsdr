import SwiftUI

/// Hardware IF offset: RX DDS (LO) = VFO + 30500 Hz.
private let IF_OFFSET_HZ: Double = 30500.0

/// Real-time spectrum line plot (FFT) with EMA smoothing, glow effect,
/// frequency grid, and VFO marker. Matches the web frontend's `_fftDraw()`.
///
/// Receives pre-processed [Float] data (0..1) from FFTProcessor.
struct FFTView: View {
    /// Smoothed, normalized spectrum data (0..1 per bin, 512 bins).
    let fftData: [Float]

    /// VFO frequency in Hz (displayed on VFO marker label).
    let rxFrequency: Int

    /// IQ sample rate in Hz (determines frequency scale).
    var iqSampleRateHz: Int = 78125

    /// Tap-to-tune callback — delivers clicked frequency in Hz.
    var onTapFrequency: ((Int) -> Void)? = nil

    private var iqSampleRate: Double { Double(iqSampleRateHz) }

    /// VFO pixel position as fraction of width (off-centre, left of LO).
    private var vfoFraction: CGFloat {
        let shift = (IF_OFFSET_HZ * 512.0 / Double(iqSampleRateHz)).rounded()
        let vfoBin = 256.0 - shift
        return CGFloat(vfoBin / 512.0)
    }

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            let h = geo.size.height
            let pad: CGFloat = 4
            let loHz = Double(rxFrequency) + IF_OFFSET_HZ
            let halfSpan = iqSampleRate / 2.0
            let span = halfSpan * 2
            let (step, _) = freqStep(span: span)
            let marks = freqLabels(forWidth: w, loHz: loHz, halfSpan: halfSpan, step: step)

            ZStack(alignment: .topLeading) {
                // ── Background ──────────────────────────────────
                Color(hex: "#020617")

                // ── FFT curve via Canvas ───────────────────────
                Canvas { ctx, size in
                    guard fftData.count >= 2 else { return }
                    let W = size.width
                    let H = size.height
                    let n = fftData.count
                    let xScale = W / CGFloat(n)

                    // ── Horizontal reference lines ─────────────
                    let refColor = GraphicsContext.Shading.color(
                        Color.white.opacity(0.05))
                    for i in 1..<6 {
                        let gy = round(pad + (H - 2 * pad) * CGFloat(i) / 6)
                        var linePath = Path()
                        linePath.move(to: CGPoint(x: 0, y: gy))
                        linePath.addLine(to: CGPoint(x: W, y: gy))
                        ctx.stroke(linePath, with: refColor, style: .init(lineWidth: 0.5))
                    }

                    // ── Frequency grid (round kHz boundaries) ──
                    let leftEdge = loHz - halfSpan
                    let gridColor = GraphicsContext.Shading.color(
                        Color.white.opacity(0.06))
                    var gf = (leftEdge / step).rounded(.down) * step
                    while gf <= loHz + halfSpan + 1 {
                        let gx = CGFloat((gf - leftEdge) / span) * W
                        if gx >= 0 && gx <= W {
                            var gridPath = Path()
                            gridPath.move(to: CGPoint(x: gx, y: 0))
                            gridPath.addLine(to: CGPoint(x: gx, y: H))
                            ctx.stroke(gridPath, with: gridColor,
                                       style: .init(lineWidth: 0.5))
                        }
                        gf += step
                    }

                    // ── Build curve path ───────────────────────
                    var curvePath = Path()
                    var firstPoint = true
                    for k in 0..<n {
                        let val = CGFloat(fftData[k])
                        let x = round(CGFloat(k) * xScale)
                        let y = pad + (H - 2 * pad) * (1.0 - val)
                        if firstPoint {
                            curvePath.move(to: CGPoint(x: x, y: y))
                            firstPoint = false
                        } else {
                            curvePath.addLine(to: CGPoint(x: x, y: y))
                        }
                    }

                    // ── Filled area ────────────────────────────
                    var fillPath = curvePath
                    fillPath.addLine(to: CGPoint(x: W, y: H - pad))
                    fillPath.addLine(to: CGPoint(x: 0, y: H - pad))
                    fillPath.closeSubpath()
                    ctx.fill(fillPath, with: .color(.cyan.opacity(0.12)))

                    // ── Glow stroke (wide, semi-transparent) ───
                    ctx.stroke(curvePath,
                        with: .color(.cyan.opacity(0.30)),
                        style: .init(lineWidth: 5.0, lineCap: .round, lineJoin: .round))

                    // ── Sharp stroke (thin, bright) ────────────
                    ctx.stroke(curvePath,
                        with: .color(Color(red: 0, green: 0.94, blue: 1.0)),
                        style: .init(lineWidth: 2.0, lineCap: .round, lineJoin: .round))
                }

                // ── VFO red marker line ────────────────────────
                Rectangle()
                    .fill(Color.red)
                    .frame(width: 1, height: h)
                    .position(x: vfoFraction * w, y: h / 2)

                // ── VFO frequency label (top) ──────────────────
                Text(String(format: "%.3f", Double(rxFrequency) / 1_000_000))
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(.red)
                    .background(Color.black.opacity(0.6))
                    .position(x: vfoFraction * w, y: 10)

                // ── Frequency tick labels (bottom) ─────────────
                ForEach(marks, id: \.hz) { mark in
                    Text(mark.label)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(.white.opacity(0.7))
                        .background(Color.black.opacity(0.5))
                        .position(x: mark.xPos, y: h - 8)
                }
            }
            .background(Color(hex: "#020617"))
            .gesture(DragGesture(minimumDistance: 0).onEnded { v in
                let fract = Double(v.location.x / w)
                let fOff = (fract - 0.5) * iqSampleRate
                let clickedFreq = Double(rxFrequency) + IF_OFFSET_HZ + fOff
                onTapFrequency?(Int(clickedFreq.rounded()))
            })
        }
    }

    // MARK: - Frequency helpers

    private struct FreqMark { let label: String; let hz: Int; let xPos: CGFloat }

    /// Choose step size based on total frequency span.
    private func freqStep(span: Double) -> (step: Double, labelEvery: Int) {
        if      span <=  50_000 { return ( 5_000, 1) }
        else if span <= 100_000 { return (10_000, 1) }
        else if span <= 200_000 { return (25_000, 2) }
        else                    { return (50_000, 2) }
    }

    /// Build frequency tick marks at round kHz boundaries, aligned with grid lines.
    /// Labels show absolute frequency in MHz (e.g. "14.100", "14.150").
    private func freqLabels(forWidth w: CGFloat, loHz: Double,
                            halfSpan: Double, step: Double) -> [FreqMark] {
        let span = halfSpan * 2
        let leftEdge = loHz - halfSpan
        var marks: [FreqMark] = []

        var f = (leftEdge / step).rounded(.down) * step
        while f <= loHz + halfSpan + 1 {
            let xPos = CGFloat((f - leftEdge) / span) * w
            let label = String(format: "%.3f", f / 1_000_000)

            if xPos >= -20 && xPos <= w + 20 {
                marks.append(FreqMark(label: label, hz: Int(f), xPos: xPos))
            }
            f += step
        }
        return marks
    }
}

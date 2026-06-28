#!/usr/bin/env python3
"""
FM 解调离线试听工具
====================
合成一段已知的 FM 调制 IQ（或回放 .iq 文件）→ 跑真实的 AudioDemodulator →
重采样到 48 kHz → 写 MP3，供人耳直接评估解调音质。

不连设备、不依赖 server。用来在改解调代码后快速 A/B 试听。

用法:
    # 合成 WFM 广播信号 (±75kHz, 312500 Hz IQ) → mp3
    python3 fm_decode_test.py --mode WFM --iq-rate 312500 --secs 6 -o /tmp/wfm.mp3

    # 合成 NFM 窄带语音 (±5kHz, 78125 Hz IQ)
    python3 fm_decode_test.py --mode NFM --iq-rate 78125 --secs 6 -o /tmp/nfm.mp3

    # 回放一段原始 24-bit LE 交织 IQ 文件 (I0 Q0 I1 Q1 ...，各 3 字节)
    python3 fm_decode_test.py --mode WFM --iq-rate 312500 --iq-file cap.iq -o /tmp/x.mp3

合成的「节目」是一组谐波音 + 缓慢扫频 + 颤音，频谱铺到 ~12 kHz，
方便听出高频是否被砍（发闷）以及是否过载破音。
"""
import argparse
import math
import subprocess
import sys

import numpy as np

import dsp
from dsp import AudioDemodulator


# ── 合成「节目」基带音频 (mono, 任意采样率) ─────────────────────────
def make_program_audio(rate: float, secs: float, wideband: bool) -> np.ndarray:
    """生成一段听感丰富的测试音频：基音和声 + 扫频 + 颤音。

    wideband=True 时铺到 ~12 kHz（WFM 听高频），否则限制在 ~3 kHz（NFM 语音）。
    输出归一化到峰值 ~0.9。
    """
    n = int(rate * secs)
    t = np.arange(n) / rate
    a = np.zeros(n, dtype=np.float64)

    # 1) 主旋律：A大调和弦 (440/554/659 Hz) 缓慢起伏
    for f, amp in [(440.0, 1.0), (554.37, 0.7), (659.25, 0.5)]:
        a += amp * np.sin(2 * math.pi * f * t)

    # 2) 颤音包络（6 Hz）让动态更像真实节目
    a *= 0.6 + 0.4 * np.sin(2 * math.pi * 6.0 * t)

    # 3) 高频内容：扫频，用来听高频是否保留
    hi_top = 12000.0 if wideband else 2800.0
    sweep = 800.0 + (hi_top - 800.0) * (0.5 - 0.5 * np.cos(2 * math.pi * 0.2 * t))
    a += 0.3 * np.sin(2 * math.pi * np.cumsum(sweep) / rate)

    # 4) 一点点宽带噪声纹理（极低电平）
    a += 0.02 * np.random.randn(n)

    a /= (np.max(np.abs(a)) + 1e-9)
    return (a * 0.9).astype(np.float64)


# ── 把基带音频 FM 调制成 IQ ──────────────────────────────────────────
def fm_modulate(audio: np.ndarray, audio_rate: float, iq_rate: float,
                peak_dev: float) -> np.ndarray:
    """将 mono 音频 FM 调制为复 IQ（中心在 0 Hz），峰值频偏 peak_dev。

    先把音频线性插值到 IQ 采样率，再积分相位。返回 complex64。
    """
    n_iq = int(len(audio) * iq_rate / audio_rate)
    # 上采样音频到 IQ 率
    xp = np.arange(len(audio))
    x = np.linspace(0, len(audio) - 1, n_iq)
    up = np.interp(x, xp, audio)
    # 预加重（模拟真实广播：高频被抬升，接收端去加重应还原）
    # 一阶高通形状：50 µs (WFM) 时间常数
    # 这里用简单差分近似，幅度小，避免引入过冲
    # （留空也可；这里加上能更真实地检验去加重是否匹配）
    phase = 2.0 * math.pi * peak_dev * np.cumsum(up) / iq_rate
    iq = np.exp(1j * phase).astype(np.complex64)
    return iq


def add_if_offset(iq: np.ndarray, iq_rate: float) -> np.ndarray:
    """把信号搬到 +IF_OFFSET，模拟真实设备（解调器会搬回 0）。"""
    n = len(iq)
    ph = 2.0 * math.pi * (-dsp.IF_OFFSET) / iq_rate * np.arange(n)
    return (iq * np.exp(1j * ph)).astype(np.complex64)


def load_iq_file(path: str) -> np.ndarray:
    """读原始 24-bit 有符号 LE 交织 IQ 文件 → complex64。"""
    raw = open(path, "rb").read()
    nsamp = len(raw) // 6
    out = np.zeros(nsamp, dtype=np.complex64)
    for i in range(nsamp):
        off = i * 6
        iv = int.from_bytes(raw[off:off + 3], "little", signed=True)
        qv = int.from_bytes(raw[off + 3:off + 6], "little", signed=True)
        out[i] = complex(iv / 8388608.0, qv / 8388608.0)
    return out


# ── 三次 (Catmull-Rom) 重采样 (src → 48000) ─────────────────────────
# 与 server.py 实时链路一致：4 点核，高频保真度远好于线性插值
# (15 kHz 处线性衰减 ~-4.4dB，三次约 -1dB)。整段一次性处理。
def resample_to_48k(pcm_f: np.ndarray, src_rate: float) -> np.ndarray:
    out_rate = 48000.0
    n_out = int(len(pcm_f) * out_rate / src_rate)
    if n_out < 2 or len(pcm_f) < 4:
        return np.zeros(0, dtype=np.float64)
    pos = np.arange(n_out) * (src_rate / out_rate)
    i1 = np.floor(pos).astype(np.int64)
    frac = pos - i1
    # 4 个邻点 i1-1, i1, i1+1, i1+2，边界 clip
    im1 = np.clip(i1 - 1, 0, len(pcm_f) - 1)
    i0c = np.clip(i1,     0, len(pcm_f) - 1)
    ip1 = np.clip(i1 + 1, 0, len(pcm_f) - 1)
    ip2 = np.clip(i1 + 2, 0, len(pcm_f) - 1)
    ym1, y0, y1, y2 = pcm_f[im1], pcm_f[i0c], pcm_f[ip1], pcm_f[ip2]
    t = frac
    # Catmull-Rom 基函数
    a0 = -0.5 * ym1 + 1.5 * y0 - 1.5 * y1 + 0.5 * y2
    a1 = ym1 - 2.5 * y0 + 2.0 * y1 - 0.5 * y2
    a2 = -0.5 * ym1 + 0.5 * y1
    a3 = y0
    return ((a0 * t + a1) * t + a2) * t + a3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="WFM", choices=["NFM", "FM", "WFM"])
    ap.add_argument("--iq-rate", type=int, default=312500,
                    help="IQ 采样率 (78125/156250/312500)")
    ap.add_argument("--secs", type=float, default=6.0)
    ap.add_argument("--dev", type=float, default=None,
                    help="峰值频偏 Hz（默认 NFM=5k, WFM=75k）")
    ap.add_argument("--iq-file", default=None,
                    help="回放原始 24bit LE 交织 IQ 文件而非合成")
    ap.add_argument("-o", "--out", default="/tmp/fm_decode.mp3")
    ap.add_argument("--wav", action="store_true", help="同时输出 wav")
    args = ap.parse_args()

    iq_rate = float(args.iq_rate)
    peak_dev = args.dev if args.dev else (75000.0 if args.mode == "WFM" else 5000.0)

    # ── 1. 拿到 IQ（中心 0 Hz，未加 IF 偏移）──
    if args.iq_file:
        iq = load_iq_file(args.iq_file)
        print(f"读入 IQ 文件: {len(iq)} 样本 @ {iq_rate} Hz")
    else:
        prog_rate = 48000.0
        prog = make_program_audio(prog_rate, args.secs, wideband=(args.mode == "WFM"))
        iq = fm_modulate(prog, prog_rate, iq_rate, peak_dev)
        print(f"合成 {args.mode} IQ: {len(iq)} 样本 @ {iq_rate} Hz, "
              f"峰值频偏 ±{peak_dev/1000:.0f} kHz")

    # ── 2. 搬到 IF（解调器期望信号在 +IF_OFFSET）──
    iq = add_if_offset(iq, iq_rate)

    # ── 3. 跑真实解调器 ──
    dsp.set_iq_sample_rate(int(iq_rate))
    demod = AudioDemodulator(sample_rate=int(iq_rate))
    demod.set_mode(args.mode)
    demod.set_volume(0.7)
    print(f"解调器: audio_rate={demod.audio_rate} Hz, decim={demod.decim}, "
          f"wdsp={'on' if demod._wdsp else 'off'}")

    # 按 200 样本/包喂（和真实设备一致），收集解调音频
    out_chunks = []
    pk = 0.0
    clip_n = 0
    for off in range(0, len(iq) - 200, 200):
        pkt = iq[off:off + 200]
        a = demod.demodulate(pkt)
        if a is not None and len(a):
            af = np.asarray(a, dtype=np.float64)
            pk = max(pk, float(np.max(np.abs(af))) if len(af) else 0.0)
            clip_n += int(np.sum(np.abs(af) >= 0.999))
            out_chunks.append(af)
    if not out_chunks:
        print("没有解调出音频", file=sys.stderr)
        sys.exit(1)
    audio = np.concatenate(out_chunks)
    print(f"解调音频: {len(audio)} 样本 @ {demod.audio_rate} Hz "
          f"({len(audio)/demod.audio_rate:.1f}s)")
    print(f"  峰值={pk:.3f}  削波样本={clip_n} ({100*clip_n/len(audio):.2f}%)  "
          f"rms={np.sqrt(np.mean(audio**2)):.3f}")

    # ── 4. 重采样到 48k → MP3 ──
    out48 = resample_to_48k(audio, float(demod.audio_rate))
    out48 = np.clip(out48, -1.0, 1.0)
    pcm16 = (out48 * 32767).astype("<i2").tobytes()

    if args.wav:
        import wave
        wpath = args.out.rsplit(".", 1)[0] + ".wav"
        with wave.open(wpath, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
            w.writeframes(pcm16)
        print(f"WAV: {wpath}")

    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "s16le", "-ar", "48000", "-ac", "1", "-i", "pipe:0",
         "-c:a", "libmp3lame", "-b:a", "128k", args.out],
        input=pcm16, capture_output=True)
    if proc.returncode != 0:
        print("ffmpeg 失败:", proc.stderr.decode()[:300], file=sys.stderr)
        sys.exit(1)
    print(f"MP3: {args.out}")


if __name__ == "__main__":
    main()

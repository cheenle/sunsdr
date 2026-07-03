# sunmrrc — 项目状态

更新时间: 2026-07-03

## 一句话现状

后端健康，RX 正常出声，TX 语音发射已验证通过：37 dB SNR，96% SSB 效率，零掉帧，IQ peak ~0.69 / RMS ~0.68 @ 100% drive。
iPhone HTTPS/WSS 安全上下文已解决，入口 `https://radio.vlsc.net:8889`。

---

## 变更记录

### 2026-07-03 频谱频率标注精度修复

修复了频谱/瀑布图频率标记在采样率切换时的准确性问题和多个 UI 缺陷：

**问题根因：**
1. 前端用 `parseInt('78k',10)*1000 = 78000 Hz` 近似计算频谱 span，但服务端实际 IQ 采样率为 78125 Hz（39k→39062, 156k→156250, 312k→312500），导致频率标签偏差 ~0.16%
2. FFT 曲线像素映射 `xScale = W/(n-1)` 把 512 bins 拉伸到 0..512 像素，与瀑布图/频率网格的 `bin k = 像素 k`（W/n）约定不一致，VFO 中心 bin 256 落到像素 257（网格 VFO 线在 256）
3. 采样率切换时 FFT EMA 平滑缓冲不重置，前 ~4 帧混入旧速率数据产生拖影
4. 采样率切换时瀑布图不清空，旧 span 像素残留与新频率网格错位
5. FFT dB 标签显示"0/-40/-80/-120"绝对 dB，但曲线用噪声底相对值+gamma 0.65 非线性映射，标签与数据不符
6. `controls.js` 缺少 `setSampleRate` 处理器，远程客户端（其他标签页/设备）无法同步采样率状态
7. 移动端"仅 78 kHz 已校准"警告文字过时

**修复：**
- `controls.js`: 新增 `EXACT_SAMPLE_RATES` 查找表（39062/78125/156250/312500），`_getSampleRateHz()` 改用精确值
- `controls.js`: 新增 `setSampleRate` 控制消息处理器，重置 FFT EMA、清空瀑布图、重绘频率标尺
- `controls.js`: FFT `xScale` 从 `W/(n-1)` 改为 `W/n`，bin 256→像素 256 与 VFO 线对齐
- `controls.js`: 移除 FFT 误导性 dB 数字标签，水平线改为纯视觉等距参考线
- `dsp.py`: `SpectrumProcessor` 新增 `reset()` 方法，`set_iq_sample_rate()` 中调用以清空 FFT 累积缓冲
- `mobile.js`: 删除过时的"仅 78 kHz 已校准"注释和 UI 警告

---

## 已完成功能

| 模块 | 状态 | 说明 |
|------|------|------|
| RX 音频 | ✅ | IQ 解调 → 16 kHz mono，默认 Opus (~18-24 kbps)，可切换 Int16 PCM |
| 频谱瀑布 | ✅ | 512-bin uint8，自适应底噪 + 色带渲染 |
| 频率/模式控制 | ✅ | VFO、band、mode，DSP 侧模式切换 |
| WDSP 降噪 | ✅ | NR2, NB, ANF, AGC (libwdsp 可选) |
| HTTPS/WSS | ✅ | 自动 TLS，iOS Safari 安全上下文 |
| PTT 安全 | ✅ | ACK 重试 + watchdog + 强制 RX |
| TX 语音调制 | ✅ | 已完全验证。麦克风 → SAB ring buffer (零主线程路径) → Opus → 300 Hz HPF → Python Hilbert SSB (唯一路径，WDSP C-chain 已移除) → 24-bit IQ；37 dB SNR，96% SSB 效率，零掉帧；IQ peak ~0.69, RMS ~0.68 @ 100% drive |
| TX 功率控制 | ✅ | 设备 DRIVE (0x0017)，per-band 功率面板，`band_power.json` 持久化；TX_DRIVE_GAIN=2.8 |
| TX 遥测 (0x1F00) | ✅ | off30 f32 = 正向功率 W (PEP)，off16 u16/10 = 电源电压 V，off18 f32 = PA 温度 °C；设备无反向功率字段，无法计算 SWR |
| 采样率切换 | ✅ | 39/78/156/312 kHz，0x0001 HW_INIT word[11] |
| Memory Channel | ✅ | `/api/mem_channels` GET/POST，`mem_channels.json` 持久化 |
| ATT/前置放大 | ✅ | 0=-20dB, 1=-10dB, 2=0dB, 3=+10dB |
| 重启脚本 | ✅ | `restart.sh`，按 cwd 杀进程，释放端口 |

---

## TX 链路详情

### 音频路径
```
浏览器麦克风 (48 kHz)
  → AudioWorklet (tx_capture_worklet.js) 降采样 3:1 → 16 kHz float32
  → SAB ring buffer (SharedArrayBuffer, 零主线程路径)
     AudioWorklet 直接写入 → Opus Worker 直接读取
     消除主线程 jitter, 此前是 mid-call dropout 的主因
  → OpusEncoder (worker 自有 WebSocket, 5ms flush)
  → 1 字节标签: 0x01=Opus, 0x00=PCM
  → /WSaudioTX WebSocket
  → 服务端 opus_tx_decoder 解码 (Opus) 或直通 (PCM)
  → TXModulator.feed_audio() 连续分数重采样 16k→15625
  → 20 Hz DC blocker (连续状态, 非逐帧重置 — 消除卡顿声)
  → 300 Hz HPF (4阶 Butterworth, 回收 sub-300 Hz 废能 → 约 +15% PA 净空)
  → Anti-alias LPF @ 3.6 kHz (防止 7.8k+ 混叠)
  → Overlap-save Hilbert SSB (USB/LSB) — 唯一 SSB 路径
     WDSP C-chain 已移除, Python Hilbert 是唯一调制器
  → tanh soft-limiter @ TX_IQ_PEAK (恒 1.0, 与 drive 无关)
     flat make-up gain = TX_DRIVE_GAIN × drive (无 AGC, 保真包络)
  → 24-bit IQ 封装 → 0xFFFD TX 流
```

### COEP 安全头
- `Cross-Origin-Embedder-Policy: credentialless` (非 `require-corp`)
- `Cross-Origin-Opener-Policy: same-origin`
- 目的: 允许 SharedArrayBuffer (SAB ring buffer 必需)
- 位置: `server.py` `_coop_coep_middleware()`

### 验证指标 (2026-06-30)
| 指标 | 数值 | 说明 |
|------|------|------|
| SNR | 37 dB | 远场测量 |
| SSB 效率 | 96% | 无用边带/载波抑制 |
| 掉帧 | 0 | SAB ring buffer 消除主线程 jitter |
| IQ peak | ~0.69 | @ 100% drive, TX_DRIVE_GAIN=2.8 |
| IQ RMS | ~0.68 | @ 100% drive |
| tanh 介入率 | <4% | 语音峰值温柔饱和, 无硬削波 |

### 功率控制
- 设备 DRIVE 命令 (0x0017)，byte = round(255 × √(drive%/100))
- Drive byte 放在 **trailing word**（非 payload）
- 每次 QSY / PTT assert 重发
- Per-band 功率面板 → `/api/band_power` → `band_power.json`
- TX_DRIVE_GAIN=2.8 (2026-06-29 从 3.5 调低, 减少 tanh 介入)

### 遥测 (0x1F00, 34 字节)
| 偏移 | 类型 | 字段 | 说明 |
|------|------|------|------|
| off30 | f32 | **正向功率 W (PEP)** | 单调随 drive 变化, 100%→~101W, 匹配 ExpertSDR3 自读 |
| off22 | f32 | **平均正向功率 W** | off30 的 ~1/3 (SSB 峰值/平均比) |
| off16 | u16 | **电源电压 ×10** | idle ~136 (13.6V), TX 大功率时降至 ~129 (12.9V) — 典型 PSU 降压 |
| off18 | f32 | PA 温度 °C | ~42-49°C |
| off26 | f32 | 占位常量 1.0000 | 永不变 |

设备在 RX/TX/TUNE 所有模式下都发送 0x1F00。
**设备无反向功率字段, 无法计算 SWR** — off16 是电源电压, 非 SWR。需 ATR-1000 获取真实 SWR。

---

## RX 编解码

- 默认 Opus (~18-24 kbps, >10× 比 PCM 省带宽)
- 每帧 1 字节标签 (`0x00`=PCM, `0x01`=Opus)，菜单 **Audio Codec** 可实时切换
- 服务端编码: `web_control/opus_rx.py`，直接 ctypes 绑定 libopus
- arm64 上 `opus_encoder_ctl` 变参调用不可用，通过 `max_data_bytes` 控码率
- 前端解码: WASM OpusDecoder (`modules/opus_wasm.js` + `opus_codec.js`)

---

## 前端代码清理 (已完成)

1. 删除死代码 `static/audio_rx.js`
2. 删除孤立的 Opus 协商代码 (已改为标签式双编解码)
3. 统一 WDSP 入口到主界面 DSP 按钮面板
4. 统一 NF 按钮绑定 (addEventListener 替代内联 onclick)
5. `AUDIO_TAG_PCM` / `AUDIO_TAG_OPUS` 提升至全局作用域
6. `tx_button.js` 预热帧添加 1 字节标签

---

## 未决

- **ATR-1000 天调**: `/WSATR1000` 是 stub，只收不发。真实 SWR 在发射期间需从天调获取
- **网络配置**: IP 地址硬编码 (192.168.16.100/200)
- **CW/FT8 页面**: 死链已从菜单移除
- **ATR-1000 后端**: `/WSATR1000` 接受连接但无真实硬件接口，SWR 需从此获取

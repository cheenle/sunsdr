# sunmrrc — 项目状态

更新时间: 2026-06-24

## 一句话现状

后端健康，RX 正常出声，TX 语音发射完成端到端验证（Tune ~12W，SSB voice 30–40W PEP）。
iPhone HTTPS/WSS 安全上下文已解决，入口 `https://radio.vlsc.net:8889`。

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
| TX 语音调制 | ✅ | 麦克风 → Opus/PCM → Hilbert SSB → 24-bit IQ，端到端验证 |
| TX 功率控制 | ✅ | 设备 DRIVE (0x0017)，per-band 功率面板，`band_power.json` 持久化 |
| TX 遥测 (0x1F00) | ✅ | off16 u16/100 = SWR (1.32-1.37)，off14 = 功率原始值，off18 = 温度 |
| 采样率切换 | ✅ | 39/78/156/312 kHz，0x0001 HW_INIT word[11] |
| Memory Channel | ✅ | `/api/mem_channels` GET/POST，`mem_channels.json` 持久化 |
| ATT/前置放大 | ✅ | 0=-20dB, 1=-10dB, 2=0dB, 3=+10dB |
| 重启脚本 | ✅ | `restart.sh`，按 cwd 杀进程，释放端口 |

---

## TX 链路详情

### 音频路径
```
浏览器麦克风 (48 kHz)
  → AudioWorklet (tx_capture_worklet.js) 降采样 3:1 → 16 kHz Int16
  → OpusEncoder 编码 (encode=true) 或直传 PCM
  → 1 字节标签: 0x01=Opus, 0x00=PCM
  → /WSaudioTX WebSocket
  → 服务端 opus_tx_decoder 解码 (Opus) 或直通 (PCM)
  → TXModulator.feed_audio() 连续分数重采样 16k→15625
  → Overlap-save Hilbert SSB (USB/LSB)
  → 24-bit IQ 封装 → 0xFFFD TX 流
```

### 功率控制
- 设备 DRIVE 命令 (0x0017)，byte = round(255 × √(drive%/100))
- Drive byte 放在 **trailing word**（非 payload）
- 每次 QSY / PTT assert 重发
- Per-band 功率面板 → `/api/band_power` → `band_power.json`

### 遥测 (0x1F00, 34 字节)
| 偏移 | 类型 | 字段 | 范围 |
|------|------|------|------|
| off14 | u16 | pwr_raw (功率包络) | idle ~9, TX 47-77 |
| off16 | u16 | **SWR × 100** | 132-137 → 1.32-1.37 |
| off18 | f32 | 温度 °C | ~45-49°C |
| off26 | f32 | 占位常量 1.0000 | 永不变 |

设备在 RX/TX/TUNE 所有模式下都发送 0x1F00 (已验证: 273 TX 态报文)。

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
- **TX 遥测功率公式**: 三次方拟合 `(pwr_raw-9)³ × 1.91e-5`，需功率表校准

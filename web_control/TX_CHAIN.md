# SunSDR2 DX — 发射 (TX) 链路

## 协议规范（抓包验证）

### TX 控制面 (Port 50001)

TX ON 不需要任何 50001 控制命令——设备检测到 PC 从端口 50002 发送 sub=0xFFFD 流后自动切到发射模式。

```
PTT ON:  无需 50001 命令（仅心跳 0x0018 继续）
PTT OFF: 0x0006(val=0, trailing word) + 0x0020(stream restore rx=0,tx=1)
```

### TX 数据面 (Port 50002)

| 字段 | Offset | 大小 | 值 | 说明 |
|------|--------|------|-----|------|
| Magic | 0 | 2 | 0xFF32 | |
| Sub-ID | 2 | 2 | **0xFFFD** | RX 是 0xFFFE |
| Counter | 4 | 4 | uint32 LE | 步长 0x10000, session 0x04B0 |
| Flags | 8 | 2 | **0x0102** | RX 是 0x0001 |
| IQ Data | 10 | 1200 | 24-bit signed LE | 200 采样 × 6 字节 |

完整包大小: 1210 字节 = 10 (header) + 1200 (IQ data)
包速率: ~440/sec @ 78125 Hz IQ rate

### IQ 数据格式

```
每个采样 (6 bytes):
  [0:3] I 分量, 24-bit signed LE, ±2^23 = ±8388608
  [3:6] Q 分量, 24-bit signed LE
```

ExpertSDR3 TX 包示例 (前 40 字节):
```
32ff fdff b004 0000 0201 00000000000000000000...  ← 静音包
32ff fdff b004 0100 0201 d59400d59400b37c00...  ← 带调制音频
```

## 当前代码链路

### 发送路径

```
用户点击 PTT
  → WebSocket "set_ptt:true"
  → server.py: radio.set_ptt(True)
  → sunsdr_direct.py: build_packet(0x0006, payload=0, trailing=1)
  → 设备 TX 灯亮
  → IQ loop 检测 _ptt_active=True
  → dsp.get_tx_iq() → 700Hz 测试音调 IQ
  → 编码为 24-bit → 发到 50002
```

### 接收路径 (WSaudioTX)

```
浏览器麦克风
  → getUserMedia() → AudioContext
  → Float32 PCM @ 浏览器采样率
  → WebSocket 二进制帧 → /WSaudioTX
  → 当前被丢弃 (未接入 TX 调制器)
```

## 文件清单

| 文件 | 函数/行 | 作用 | 状态 |
|------|---------|------|------|
| `sunsdr_direct.py:set_ptt()` | PTT 控制 | build_packet(0x0006, payload=0, trailing=1/0) | ✅ |
| `server.py:84-91` | TX IQ 发送 | get_tx_iq() → 50002 | ✅ |
| `dsp.py:TXModulator` | 音频→IQ | generate_test_tone() / feed_audio() | 🔶 |
| `server.py:WSaudioTX` | 浏览器麦克风 | 接收 PCM, 未接入 | ❌ |

## 测试方法

### 前提条件
- 服务已启动：`cd web_control && WEB_PORT=8889 bash restart.sh`
- 设备 TX 灯在 PTT 后能亮
- 有一台独立接收机，调到相同频率（如 7.074 MHz），模式 USB

### 测试1: 检查 TX 包内容

```bash
# 抓包确认 TX 包是否有非零数据
sudo tcpdump -i en0 -c 50 -X "host 192.168.16.200 and udp and port 50002" &
PID=$!
curl -X POST http://localhost:8889/api/ptt/on
sleep 3
curl -X POST http://localhost:8889/api/ptt/off
sudo kill $PID
# 在抓包里找 sub=0xFFFD 的包，检查 IQ 数据是否非零
```

### 测试2: 接收机听 700Hz 测试音调

```bash
# 1. 确认服务运行 http://localhost:8889
# 2. 旁边接收机调到 7.074 MHz USB
# 3. 触发 PTT
curl -X POST http://localhost:8889/api/ptt/on
# 4. 听 5 秒
sleep 5
# 5. 关 PTT
curl -X POST http://localhost:8889/api/ptt/off
```

**预期**：接收机在 USB 模式下听到 700Hz 持续音调。如果听到 → TX 链路完整。如果没听到 → TX 包内容仍为全零，需检查 `dsp.get_tx_iq()` 返回值。

### 测试3: 浏览器麦克风

```bash
# 打开 http://localhost:8889
# 1. 浏览器会请求麦克风权限（getUserMedia）
# 2. 允许后，点击 PTT 按钮
# 3. 对着麦克风说话
# 4. 接收机应能听到声音
```
当前状态：浏览器麦克风数据到达 `/WSaudioTX` 但未接入 TXModulator。需将 `WSaudioTX` 收到的 Float32 PCM 喂入 `TXModulator.feed_audio()`。

### 测试4: 链路端点验证

```bash
# 状态
curl http://localhost:8889/api/status
# → {"connected":true, "dsp":true, "clients":{...}}

# 设频
curl -X POST http://localhost:8889/api/ptt/on
# → {"ok":true,"ptt":true}
```

## 待完成

1. **验证测试音调** — 启动服务, PTT, 接收机听 700Hz
2. **Hilbert SSB 调制** — `scipy.signal.hilbert` 替代导数近似
3. **WSaudioTX 接入** — 浏览器 Float32 PCM → TXModulator.feed_audio()
4. **音频重采样** — 浏览器 rate → 16000 → 78125

# TX 发射链路完善计划

> **状态: ✅ 全部完成 (2026-06-24)**
> 
> 所有 Phase 0-4 已实施并通过端到端验证。此文档保留为历史参考。

## 最终状态

| 项目 | 状态 | 说明 |
|------|------|------|
| TUNE 功能 | ✅ | PTT 激活 + WAV 循环发射，Tune ~12W |
| DSP 调制器 | ✅ | Hilbert SSB，USB/LSB/AM/FM/CW |
| 实时麦克风 TX | ✅ | Opus/PCM tagged uplink → `feed_audio()` → SSB IQ |
| 协议修复 | ✅ | 心跳尾部 8B、复用 radio._sock |
| TX 功率控制 | ✅ | DRIVE 0x0017 trailing word，per-band 面板 |
| TX 遥测 | ✅ | 0x1F00 off16 u16/100 = SWR |
| 前端 TX | ✅ | AudioWorklet + Opus 编码 + 标签字节 |
| 噗噗声消除 | ✅ | Jitter buffer + TX ramp + 自适应 pacer |

## 关键经验

1. **TX IQ 电平**：ExpertSDR3 pcap 显示 ~0.005，但硬件实际需要 ~0.3 才有功率输出
2. **测试音 > WAV 预处理**：相位连续复指数（`generate_test_tone`）最干净；Hilbert + 上采样引入微小 AM 波动
3. **频域 SSB**（FFT→零负频→补零→IFFT）理论上最干净，但 `irfft`/`ifft` 实现复杂，未调通
4. **包时序**：asyncio 批量发包 > asyncio 逐包 > 线程逐包，但仍有微抖动
5. **Hilbert 零填充**：`hilbert(x, N=len(x)+pad)` 比 `np.pad` + `hilbert` 干净

### 表层问题（TX 链路断点）
- `/WSaudioTX` WebSocket 收到音频直接丢弃
- `_process_iq_stream()` PTT 分支发送 1200 字节零值（静音）
- `TXModulator._modulate_usb()` 用导数近似替代 Hilbert 变换，SSB 质量差
- `set_tune()` 是空函数

### 深层问题（设备协议 bug — 与 web_control/server.py 对比发现）

| # | Bug | 位置 | 严重度 |
|---|-----|------|--------|
| B1 | **TX IQ 数据全为零** — 调用 `b'\x00'*1200` 而非 `dsp_proc.get_tx_iq()` | sunmrrc/server.py:177 | 🔴 **致命** |
| B2 | **心跳包尾部截断** — sunmrrc 发 4 字节尾部，桌面版发 8 字节 (`<II`) | sunmrrc/server.py:150 | 🔴 **致命** — 8 分钟会话超时断连 |
| B3 | **心跳从错误源端口发出** — 单独创建 socket bind 50001 失败后降级到随机端口 | sunmrrc/server.py:144-148 | 🟡 **高** |
| B4 | **关机不释放 PTT** — lifespan 不调用 `radio.disconnect()` | sunmrrc/server.py:54-58 | 🟡 **高** |
| B5 | **TX 期间不发 RX 流保活包** — PTT 分支 `continue` 跳过 0xFFFE 逻辑 | sunmrrc/server.py:179 | 🟡 **高** |
| B6 | **`_ptt_active` 未初始化** — 仅在 `set_ptt()` 创建 | sunsdr_direct.py | 🟢 低 |
| B7 | **tx_counter 不复位** — 模块级变量跨重连不重置 | sunmrrc/server.py:29 | 🟢 低 |
| B8 | **HW_INIT 尾部截断** — 启动 hex 字面量少 2 字节 (`c025` 应为 `c0250000`) | sunsdr_direct.py:158 | 🟢 低 |

---

## Phase 0: 协议 Bug 修复

**必须在 TX 链路实现之前修复**，否则发射可能仍然不正常。

### 修改文件: `sunmrrc/server.py`, `web_control/sunsdr_direct.py`

### 0.1 心跳包尾部长度修正 (B2)
**位置**: `sunmrrc/server.py` ~line 150

当前:
```python
hdr = struct.pack("<HHIIH", 0xFF32, 0x0018, 4, 0x00010000, 0) + struct.pack("<I", 0)
```
修正（匹配 web_control 的 8 字节尾部）:
```python
hdr = struct.pack("<HHIIH", 0xFF32, 0x0018, 4, 0x00010000, 0) + struct.pack("<II", 0, 0)
```

### 0.2 心跳复用 radio._sock 而非独立 socket (B3)
**位置**: `sunmrrc/server.py` ~lines 139-156

去掉独立 socket 创建/bind 逻辑，改用 `radio._sock.sendto(...)`，匹配 web_control 行为。

### 0.3 关机时释放 PTT + 断开连接 (B4)
**位置**: `sunmrrc/server.py` lifespan yield 后 (~line 52)

新增:
```python
yield
# Shutdown
if getattr(radio, '_ptt_active', False):
    logger.warning("Shutdown: forcing PTT release")
    await radio.set_ptt(False)
await radio.disconnect()
```

### 0.4 TX 分支中维持 RX 流保活 (B5)
**位置**: `sunmrrc/server.py` ~lines 174-179

PTT 分支发完 TX 包后，新增 0xFFFE 保活检查（复用 `last_keepalive`），确保退出 TX 后 RX 流正常。

### 0.5 `_ptt_active` 初始化 (B6)
**位置**: `web_control/sunsdr_direct.py` `__init__`
```python
self._ptt_active = False
```

### 0.6 HW_INIT 尾部补齐 (B8)
**位置**: `web_control/sunsdr_direct.py` ~line 158
```python
# 当前结尾: "...000000000000c025"
# 修正为:   "...000000000000c0250000"
```

---

## Phase 1: DSP 调制器升级 (`web_control/dsp.py`)

### 1.1 新增 import
```python
from scipy.signal import hilbert
```

### 1.2 扩展 `TXModulator.__init__()`（~line 361）
新增字段: `self._tune_active = False`, `self._tune_freq = 700.0`

### 1.3 替换 `_modulate_usb()` 为 `_modulate()`（删除 line 404-429）
用 `scipy.signal.hilbert` 实现正确的 SSB 调制:

```python
def _modulate(self, audio: np.ndarray) -> np.ndarray | None:
    """40 audio samples → 200 IQ samples. 支持 USB/LSB/AM/FM/CW."""
    xp = np.linspace(0, 1, len(audio))
    x  = np.linspace(0, 1, 200)
    upsampled = np.interp(x, xp, audio).astype(np.float64)

    if self.mode == "USB":
        analytic = hilbert(upsampled)
        iq = analytic.astype(np.complex128)
    elif self.mode == "LSB":
        analytic = hilbert(upsampled)
        iq = np.conj(analytic).astype(np.complex128)
    elif self.mode == "AM":
        envelope = np.abs(upsampled) - np.mean(np.abs(upsampled))
        iq = envelope.astype(np.complex128)
    elif self.mode in ("FM", "NFM"):
        phase = np.cumsum(upsampled) * 0.3 / 200.0
        iq = 0.3 * np.exp(1j * phase)
    elif self.mode == "CW":
        t = np.arange(200, dtype=np.float64) / self.iq_rate
        iq = 0.3 * np.exp(2j * np.pi * self._tune_freq * t)
    else:
        return None

    peak = np.max(np.abs(iq))
    if peak > 1e-6:
        iq = (iq / peak) * 0.3  # 30% modulation
    return iq.astype(np.complex64)
```

### 1.4 新增方法
```python
def set_tune(self, active: bool, freq_hz: float = 700.0):
    self._tune_active = active
    self._tune_freq = freq_hz

def generate_cw_iq(self) -> bytes:
    """Generate 1200-byte CW carrier IQ packet."""
    t = np.arange(200, dtype=np.float64) / self.iq_rate
    iq = (0.3 * np.exp(2j * np.pi * self._tune_freq * t)).astype(np.complex64)
    return encode_tx_iq_packet(iq)
```

### 1.5 重写 `StreamProcessor.get_tx_iq()`（line 480-483）
三重优先级:
1. TX 音频 → `self.modulator.feed_audio(pcm)` → 调制 IQ
2. Tune 模式 → `self.modulator.generate_cw_iq()` → CW 载波
3. 兜底 → `return None`（调用方填充零值）

---

## Phase 2: 服务端音频通路 (`sunmrrc/server.py`)

### 2.1 新增音频队列（~line 29）
```python
from collections import deque as collections_deque
tx_audio_deque = collections_deque(maxlen=200)  # ~4 秒缓冲
```

### 2.2 注入队列到 StreamProcessor（lifespan, ~line 47）
```python
dsp_proc._tx_audio_deque = tx_audio_deque
```

### 2.3 实现 `/WSaudioTX` handler（替换 line 482-488）
- 解析 text 消息: `"s:"` → 紧急 PTT 释放, `"m:..."` → 日志记录
- 解析 binary 消息: Int16 PCM → push 到 `tx_audio_deque`

### 2.4 PTT 分支发送真实 IQ（替换 line 174-179）
```python
if getattr(radio, '_ptt_active', False):
    tx_counter += 0x10000
    hdr = struct.pack("<HHIH", 0xFF32, 0xFFFD, tx_counter, 0x0102)
    tx_data = dsp_proc.get_tx_iq() if dsp_proc else None
    if tx_data is None:
        tx_data = b'\x00' * 1200
    if len(tx_data) < 1200: tx_data += b'\x00' * (1200 - len(tx_data))
    elif len(tx_data) > 1200: tx_data = tx_data[:1200]
    try: iq_sock.sendto(hdr + tx_data, (DEVICE_HOST, 50002))
    except: pass
    # 0xFFFE 保活（见 Phase 0.4）
    ...
    await asyncio.sleep(0.0022); loop_counter += 1; continue
```

### 2.5 PTT 释放时清理音频缓冲
```python
if not tx:
    tx_audio_deque.clear()
    dsp_proc.modulator._audio_buf = np.array([], dtype=np.float32)
```

### 2.6 Tune handler
同步 `dsp_proc.modulator.set_tune(tune_on)`

---

## Phase 3: `set_tune()` 实现 (`web_control/sunsdr_direct.py`)

替换 line 253-254 的空函数:
```python
async def set_tune(self, enable: bool):
    self._tune_active = enable
    await self.set_ptt(enable)
```

---

## Phase 4: 前端 PCM 强制（可选）

隐藏/禁用 index.html 中的 Opus 编码复选框，确保 PCM-only 传输。

---

## 修改量汇总

| Phase | 文件 | 行数变化 | 风险 |
|-------|------|---------|------|
| **0** | `sunmrrc/server.py` + `sunsdr_direct.py` | ~40 行 | 🔴 高 — 协议兼容性 |
| 1 | `web_control/dsp.py` | ~50 行 | 中 — 调制器核心 |
| 2 | `sunmrrc/server.py` | ~50 行 | 中 — 数据通路 |
| 3 | `web_control/sunsdr_direct.py` | ~3 行 | 低 |
| 4 | `sunmrrc/static/index.html` | 1 行（可选） | 极低 |

## 不变更的文件
- `sunmrrc/static/controls.js` — 前端 TX 音频捕获完全可用
- `sunmrrc/static/tx_button.js` — PTT 状态机和 watchdog 正常
- `sunmrrc/static/mobile.js`
- PTT 安全机制: ACK 重试、`s:` 紧急释放、watchdog 全部保持

## 验证计划

1. 不按 PTT，确认 server.log 无异常，RX 正常
2. 抓包确认 0x0018 心跳尾部为 8 字节、源端口为 50001
3. 按 Tune → 用另一接收机在 700Hz 偏移处验证 CW 载波
4. 按 PTT 说话 → 用另一接收机验证 SSB 解调质量
5. 持续 PTT >30 秒 → 确认不掉会话、退出 TX 后 RX 恢复
6. TX 状态下重启服务 → 确认设备回到 RX

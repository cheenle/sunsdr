# SunMRRC — SunSDR2 DX 移动端 Web 控制

## 架构

```
手机浏览器 ←WebSocket→ Python FastAPI ←UDP→ SunSDR2 DX
 MRRC移动UI         sunmrrc/server.py       硬件
```

## 当前功能

| 功能 | 状态 | 备注 |
|------|------|------|
| 频率显示/设置 | ✅ | getFreq/setFreq via WSCTRX |
| 模式切换 | ✅ | USB/LSB/CW/AM/FM |
| PTT 发射 | ✅ | TX 灯亮，RF 输出 |
| S 表 | ✅ | FFT 频谱 90 分位 |
| 音频播放 | ✅ | Int16 PCM @16000Hz via WSaudioRX |
| 音频采集 | 🔶 | WSaudioTX 已就绪，待接入 TX IQ 调制 |
| 瀑布图 | ❌ | MRRC UI 无瀑布组件 |
| DSP NR2/ANF | 🔶 | WDSP 库就绪，待稳定集成 |

## 端点

| 路径 | 协议 | 用途 |
|------|------|------|
| `/` | HTTP | 移动 UI (MRRC mobile_modern) |
| `/WSCTRX` | WebSocket 文本 | 控制命令 (setFreq:/setMode:/setPTT:) |
| `/WSaudioRX` | WebSocket 二进制 | 接收音频 (Int16 PCM, 16000Hz) |
| `/WSaudioTX` | WebSocket 二进制 | 发射音频 (Float32 PCM, 16000Hz) |
| `/{file}` | HTTP | 静态资源 (CSS/JS/模块) |

## 控制命令 (WSCTRX)

| 命令 | 示例 | 说明 |
|------|------|------|
| getFreq | `getFreq:` | 查询频率 |
| getMode | `getMode:` | 查询模式 |
| getPTT | `getPTT` | 查询 PTT 状态 |
| setFreq | `setFreq:7150000` | 设置频率 (Hz) |
| setMode | `setMode:USB` | 设置模式 |
| setPTT | `setPTT:true` | PTT 开关 |
| tune | `tune:true` | 调谐模式 |
| setAFGain | `setAFGain:50` | 音频增益 |
| setRFGain | `setRFGain:80` | RF 增益 |
| setFilter | `setFilter:200,2800` | 滤波带宽 |
| setAGC | `setAGC:SLOW` | AGC 模式 |
| setPreamp | `setPreamp:true` | 前置放大 |
| getSignalLevel | `getSignalLevel:` | S 表 (自动推送) |

## 待完成

1. **WDSP 稳定集成** — C 库在填充块上输出零，需调试 ctypes 调用
2. **TX 音频调制** — 浏览器麦克风 → TX IQ → 50002 流
3. **瀑布图** — MRRC UI 无此组件，需自行开发或从 web_control 移植
4. **独立启动** — 当前复用 web_control 的 sunsdr_direct/dsp 模块

## 启动

```bash
cd sunmrrc
/Users/cheenle/HAM/sunsdr/py311_env/bin/python server.py
# 打开 http://localhost:8080
```

## 依赖

- Python: fastapi, uvicorn, numpy, scipy, websockets
- 系统: libwdsp.dylib (可选，/usr/local/lib/)
- 同项目: web_control/sunsdr_direct.py, web_control/dsp.py

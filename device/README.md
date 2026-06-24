# device/ — SunSDR2 DX 抓包基础数据与分析

本目录保存从 **ExpertSDR3 官方软件** 抓取的发射链路 UDP 报文，以及解析脚本和提炼后的结论，
作为后续深入分析发射协议的**基准数据集**。所有结论均可由 `captures/` 中的 pcap 复现。

## 目录结构

```
device/
├── README.md              本文件
├── captures/              原始 pcap（ExpertSDR3 真实发射）
│   ├── sunsdr_sdr_tx.pcap     10MB — 3 次 PTT 发射的完整抓包（主参考）
│   └── sunsdr_tx_full.pcap    7MB  — 另一段发射会话
├── data/
│   ├── tx_analysis.json       机器可读的全部 TX 发现（由 analyze_tx.py 生成）
│   └── expert_manual.txt      ExpertSDR3 中文手册纯文本（硬件约束参考）
└── scripts/
    ├── analyze_tx.py          ★ 主分析脚本，复现 tx_analysis.json
    ├── pcap_tx.py             子类型/方向直方图
    ├── pcap_tx2.py            0xFFFD 计数器 + PTT 事件
    ├── pcap_tx3.py            burst 包络分析
    └── pcap_tx4.py            burst 速率/间隔统计
```

## 复现

```bash
python3 device/scripts/analyze_tx.py device/captures/sunsdr_sdr_tx.pcap
# → 重新生成 device/data/tx_analysis.json
```

脚本只依赖 `numpy`，自带最小 pcap 解析器（处理 Ethernet/IPv4/UDP），不需要 scapy/tshark。

## 核心发现（详见 data/tx_analysis.json 与 PROTOCOL.md §17）

| 项 | 实测值 | 现有代码假设 | 影响 |
|----|--------|--------------|------|
| **TX IQ 采样率** | **39,063 Hz** (5⁷/2 = RX/2) | 78,125 Hz | 🔴 包速率 2× 过快，缓冲溢出 → 噗噗声 |
| **TX IQ 峰值电平** | **~0.092** | 0.3 | 🔴 0.3 触发硬件 ALC/限幅 → 失真 |
| TX 包间隔（中位） | 5.12 ms | 2.56 ms | 同上（速率问题） |
| RX IQ 采样率 | 78,125 Hz | 78,125 Hz | ✓ 一致 |
| 发射前导静音 | ~17 包（~87ms） | 直接灌 IQ | PA/继电器 settling |
| 流包字节 4-7 | 包计数器（每包 +0x10000，PTT 复位 0x04B0） | 误标为 data_len | 文档修正 |
| 0xFFFD : 0xFFFE | TX 期间 ~8:1 交错 | — | 保活机制 |

### 数据陷阱（记录以免重蹈）

计数器在**每次 PTT 复位为 0x04B0**，所以 3 个 burst 的计数器值重复。
**对计数器做全局去重会把帧数错误地砍半**（曾误得 78kHz）。
正确做法：用**逐帧在途间隔中位数（5.12ms）**估算速率，不要去重。

### 设备→PC 遥测（次要，待深入）

- `sub=0x1F00`（34B）：含浮点 45.0、1.0 — 疑似 PA 温度/正向功率/SWR
- `sub=0x1F01`（22B）：基本全零 — 疑似 TX 状态标志

## 手册（expert_manual.txt）相关约束

- IQ 采样率档位：**39 / 78 / 156 / 312 kHz @ 24bit**（确认 39063 是最低档）
- 每电台分配 2 个 UDP 端口（默认 50001 控制 / 50002 IQ 流）
- ALC 功能"当前固件尚不支持" → 不能靠硬件 ALC 限幅，**发射电平必须软件端控制**
- PTT 切换有延迟，外置功放需前导时间 → 印证发射前导静音

## 实现状态（已落地到代码）

基于本目录的分析，以下修复已应用到 `web_control/dsp.py` 与 `sunmrrc/server.py`
（详见 PROTOCOL.md §17.8）：

| 修复 | 位置 | 说明 |
|------|------|------|
| TX 速率 5.12ms/包 | `server.py` `_tx_pacer_thread` | 专用 OS 线程，39063 Hz 节拍 |
| TX 峰值电平 | `dsp.py` `TX_IQ_PEAK` | 当前调到 **0.4** 试验（抓包参考 ~0.09，无固件 ALC，需真机标定）|
| 发射前导静音 | `server.py` `TX_SETTLE_PACKETS=17` | PTT 后先发 17 个零 IQ 包 |
| 计数器线程安全 | `server.py` `_next_tx_counter()` + lock | 0xFFFD/0xFFFE 共享计数器加锁，消除竞争丢包 |
| 起始幅度斜坡 | `dsp.py` `_apply_ramp()` | 静音垫→满幅线性淡入，消硬跳变咔哒 |
| Mic→TX 通路 | `server.py` `/WSaudioTX` + `dsp.py` `feed_audio()` | 之前 mic 帧被丢弃、TX 发 700Hz 测试音；现接入真实语音 |
| 连续重采样 + jitter buffer | `dsp.py` `feed_audio`/`get_mic_iq` | overlap-save SSB，先攒 12 包再放，消欠载咔哒 |
| LSB/USB 边带 | `dsp.py` `set_mode`/`feed_audio`/`set_tune_wav` | TX 调制器现按 mode 选边带（conj=LSB）|
| 前端改发 PCM | `static/index.html` `#encode` 去掉 checked | 前端原默认 Opus，后端只认 Int16 PCM；改为 PCM 直发 |
| 设备遥测解码 | `server.py` 0x1F00 分支 | power@off14 / temp@off18 / swr@off16 (u16/100) → 前端 ATR 显示 |

> ⚠️ `TX_IQ_PEAK=0.4` 与遥测功率标定（`TELEM_PWR_BASELINE/SCALE`）均为试验值，
> 需在真机上对照已知输出功率/SWR 校准。

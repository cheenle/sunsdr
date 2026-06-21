# sunmrrc 手机侧 RX 链路 — 阶段性状态

更新时间:2026-06-21

## 一句话现状
后端健康、本地电脑 RX 出声 + 频率/模式控制正常。
iPhone 无声音/无麦克风/控制不工作的根因 = **HTTP 不是 iOS 安全上下文** →
已切换到 **HTTPS(wss)** 解决。访问入口:`https://radio.vlsc.net:8889`。

---

## 已完成的前端梳理(第一轮)

四项改动,均通过 `node --check`,引用完整性已核对:

1. **删除死代码 `static/audio_rx.js`** —— 未被 index.html 加载,与 controls.js 函数重名。
2. **固定 Int16 PCM,移除 Opus 协商**(`static/controls.js`):
   - 删 `wsAudioRXopen` 里发 `set_opus_encode` 的请求;
   - 两处 `onmessage` 去掉按帧大小猜格式的分支,直接 `decodeInt16Audio`;
   - 删孤立的 `decodeOpusAudio` 和 `AudioRX_OpusDecoder/AudioRX_opusDecode` 全局;
   - 码率显示文案固定 "Int16"。
3. **统一 WDSP 入口到主界面 DSP 按钮面板**(`static/mobile.js`):
   - 删设置菜单里重复的 WDSP 区块;
   - 删 `showWDSPAdvancedSettings()` 及其专属的
     `addWDSPNotchFromUI / updateNFNotchesList / deleteWDSPNotchFromUI`;
   - 保留主面板链路全部函数(含 `setWDSPNR2Level`)。
4. **统一 NF 按钮绑定**:去掉 index.html 的内联 `onclick`,改在
   `initDSPControlPanel()` 用 `addEventListener` 绑定,与其余四键一致。

---

## 已完成:HTTPS/TLS 改造(第二轮 — 修复 iPhone)

### 根因
iOS Safari 只把 **HTTPS 源**当作安全上下文。手机用 `http://192.168.1.64:8889` 打开时:
- `navigator.mediaDevices` 为 `undefined` → `getUserMedia` 无法调用 → **不弹麦克风授权**;
- AudioContext 自动播放解锁在 HTTP 下不可靠 → 数据进来了(250K≈256kbps Int16 PCM)却**没声音**。

(注:日志显示手机 `/WSaudioRX`、`/WSaudioTX` 都连上了;`/WSATR1000` 403 是预期的——
该端点从 MRRC 拷来、后端尚未实现,未来会继承使用,不是 bug。)

### 改动
1. **`server.py`**:加 `_find_ssl()`,检测到 `certs/fullchain.pem` + `certs/radio.vlsc.net.key`
   即以 HTTPS 启动(`ssl_certfile`/`ssl_keyfile`)。`DISABLE_SSL=1` 可强制 HTTP(本地调试)。
2. **`static/controls.js`**:四处 WebSocket 地址 `'ws://'` 改为
   `(location.protocol === 'https:' ? 'wss://' : 'ws://')` —— HTTPS 页面自动用 wss,
   消除混合内容拦截。覆盖 WSaudioRX / WSCTRX / WSaudioTX(含重连分支)。

### 证书(已就位,EC 256-bit)
- `certs/fullchain.pem`:CN=radio.vlsc.net,有效期 2026-06-12 → 2026-09-10。
- `certs/radio.vlsc.net.key`:与证书公钥 MD5 一致,配对验证通过。

### 验证(本机)
- 日志:`sunmrrc https://0.0.0.0:8889` + `TLS: .../fullchain.pem`;
  `Uvicorn running on https://0.0.0.0:8889`。
- `https://localhost:8889` → HTTP 200;用 `radio.vlsc.net` 域名校验 → HTTP 200(证书链完整)。
- 后端 SunSDR/IQ 流正常。

### 待你在 iPhone 上确认
- 用 `https://radio.vlsc.net:8889` 打开(域名要解析到这台机器)。
- 开机后应:出声 + 弹麦克风授权 + 控制可用。

---

## 后端(健康,RX 处理未改动)
直接 import 桌面版 `web_control/dsp.py` + `sunsdr_direct.py`,同一套 RX 处理。
启动确认:`SunSDR2DX: True`、`WDSP ready`、`SR=15625Hz`、`IQ: port 50002` 有流量。

---

## 运维:重启脚本
`sunmrrc/restart.sh` —— 按 cwd 精确杀本目录 server.py(不误伤 web_control 的),
释放端口(仅 LISTEN 套接字,不误杀浏览器客户端连接)后后台重启,日志写 `sunmrrc/server.log`。
- `./restart.sh`(默认 8889) / `WEB_PORT=8889 ./restart.sh` / `./restart.sh -f`(前台)
- 本地 HTTP 调试:`DISABLE_SSL=1 ./restart.sh`

---

## 后续 / 未决
- **WSCTRX 控制**:网络层(地址/代理)后续再调,用户已明确不是当前重点。
- **TX 麦克风**:HTTPS 下 `getUserMedia` 才可用;后端 TX 音频调制本身仍未实现。
- **`/WSATR1000`**:天调代理端点,未来实现。

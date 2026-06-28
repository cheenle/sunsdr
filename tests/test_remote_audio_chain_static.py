import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class RemoteAudioChainStaticTests(unittest.TestCase):
    def test_audio_worklet_modules_are_loaded_with_auth_token(self):
        controls = read("sunmrrc/static/controls.js")

        self.assertRegex(
            controls,
            r"audioWorklet\.addModule\(\s*wsUrlWithAuth\('/rx_worklet_processor\.js\?v=",
        )
        self.assertRegex(
            controls,
            r"audioWorklet\.addModule\(\s*wsUrlWithAuth\('/tx_capture_worklet\.js",
        )

    def test_tx_reconnect_updates_encoder_socket_field(self):
        controls = read("sunmrrc/static/controls.js")

        self.assertIn("ap.wsh = wsAudioTX", controls)
        self.assertNotIn("ap.ws = wsAudioTX", controls)

    def test_spectrum_fps_control_and_remote_preset_exist(self):
        server = read("sunmrrc/server.py")
        mobile = read("sunmrrc/static/mobile.js")

        self.assertIn("spectrum_fps", server)
        self.assertIn('cmd == "setSpectrumFps"', server)
        self.assertIn("setSpectrumFps:12", mobile)
        self.assertIn("Opus 32k", mobile)

    def test_opus_bitrate_menu_sends_kbps_not_bps(self):
        mobile = read("sunmrrc/static/mobile.js")

        self.assertIn("setOpusBitrate:\" + Math.round(br / 1000)", mobile)
        self.assertNotIn("setOpusBitrate:\" + br", mobile)

    def test_audio_codec_menu_controls_tx_and_rx_codec(self):
        mobile = read("sunmrrc/static/mobile.js")
        controls = read("sunmrrc/static/controls.js")

        self.assertIn("setTxOpusEnabled(false)", mobile)
        self.assertIn("setTxOpusEnabled(true)", mobile)
        select_start = mobile.index("function selectOpus(key)")
        select_end = mobile.index("// ---- 记忆管理面板 ----", select_start)
        select_fn = mobile[select_start:select_end]
        pcm_branch = re.search(
            r"if \(key === 'pcm'\) \{(?P<body>.*?)\} else \{",
            select_fn,
            re.S,
        )
        self.assertIsNotNone(pcm_branch)
        self.assertIn("setTxOpusEnabled(false)", pcm_branch.group("body"))
        opus_branch = re.search(
            r"\} else \{(?P<body>.*?)closeModalPanel\(\);",
            select_fn,
            re.S,
        )
        self.assertIsNotNone(opus_branch)
        self.assertIn("setTxOpusEnabled(true)", opus_branch.group("body"))
        self.assertIn('document.getElementById("encode")', controls)
        self.assertIn("encode = (encodeElement && encodeElement.checked) ? 1 : 0", controls)

    def test_server_accepts_legacy_bps_opus_bitrate_values(self):
        server = read("sunmrrc/server.py")

        self.assertIn("raw_kbps = int(float(val))", server)
        self.assertIn("raw_kbps > 1000", server)

    def test_remote_and_plain_opus32_are_not_both_active(self):
        mobile = read("sunmrrc/static/mobile.js")

        self.assertIn("mobileState.spectrumFps !== 12", mobile)

    def test_ios_scriptprocessor_uses_opus_jitter_gate(self):
        controls = read("sunmrrc/static/controls.js")

        self.assertIn("SCRIPT_OPUS_PREBUFFER_MS", controls)
        self.assertIn("window.__rxScriptPriming", controls)
        self.assertIn("window.__rxScriptGateSamples", controls)
        self.assertIn("window.__rxLastCodec === 'opus'", controls)

    def test_rx_opus_frame_comments_match_48khz_frames(self):
        server = read("sunmrrc/server.py")
        opus_rx = read("web_control/opus_rx.py")

        self.assertIn("960-sample (20 ms)", server)
        self.assertIn("Opus packet (decode with the frontend OpusDecoder @ 48 kHz mono)", opus_rx)

    def test_tx_opus_does_not_enable_fec_or_dtx_on_websocket(self):
        opus = read("sunmrrc/static/modules/opus_codec.js")

        self.assertRegex(opus, r"setValue\(fec_ptr,\s*0,\s*'i32'\)")
        self.assertRegex(opus, r"setValue\(dtx_ptr,\s*0,\s*'i32'\)")

    def test_tx_opus_encoder_uses_mobile_safe_complexity(self):
        opus = read("sunmrrc/static/modules/opus_codec.js")
        controls = read("sunmrrc/static/controls.js")

        self.assertRegex(opus, r"setValue\(complexity_ptr,\s*3,\s*'i32'\)")
        self.assertIn("complexity=3", opus)
        self.assertIn("complexity=3", controls)
        self.assertRegex(opus, r"setValue\(vbr_ptr,\s*0,\s*'i32'\)")
        self.assertIn("VBR=OFF", opus)

    def test_tx_opus_uses_worker_owned_websocket_not_main_thread_encoder(self):
        controls = read("sunmrrc/static/controls.js")
        worker = read("sunmrrc/static/tx_opus_worker.js")

        self.assertIn("new Worker(wsUrlWithAuth('/tx_opus_worker.js?v=", controls)
        self.assertIn("OpusEncoderProcessor.prototype.sendOpusWorkerFrame", controls)
        self.assertIn("this.sendOpusWorkerFrame(int16Frame)", controls)
        self.assertIn("type: 'frame'", controls)
        self.assertIn("type: 'stop'", controls)
        self.assertNotIn("this.opusEncoder.encode_float(f32)", controls)
        self.assertIn("importScripts(", worker)
        self.assertIn("/modules/opus_wasm.js", worker)
        self.assertIn("/modules/opus_codec.js", worker)
        self.assertIn("new WebSocket", worker)
        self.assertIn("/WSaudioTX", worker)
        self.assertIn("AUDIO_TAG_OPUS", worker)
        self.assertIn("encoder.encode_float", worker)
        self.assertNotIn("TX_OPUS_WORKER_PACE_MS", worker)
        self.assertNotIn("setTimeout(tick", worker)

    def test_tx_mic_jitter_buffer_absorbs_mobile_opus_stalls(self):
        dsp = read("web_control/dsp.py")
        server = read("sunmrrc/server.py")

        self.assertIn("TX_MIC_PRIME_PKTS = 28", dsp)
        self.assertIn("TX_MIC_REPRIME_PKTS = 14", dsp)
        self.assertIn("_Q_TARGET = 70.0", server)

    def test_server_paces_tx_uplink_frames_before_modulator(self):
        server = read("sunmrrc/server.py")

        self.assertIn("TX_WS_JITTER_PRIME_FRAMES", server)
        self.assertIn("TX_WS_JITTER_REPRIME_FRAMES", server)
        self.assertIn("async def _tx_uplink_pacer", server)
        self.assertIn("_tx_pcm_queue.append", server)
        self.assertIn("await asyncio.sleep(_tx_frame_s)", server)
        self.assertIn("_capture_tx_pcm(pcm, tag, wire_bytes, _pace_now, _pace_interval_ms)", server)

    def test_expired_auth_for_audio_assets_forces_login_not_degraded_audio(self):
        controls = read("sunmrrc/static/controls.js")
        worker = read("sunmrrc/static/tx_opus_worker.js")
        server = read("sunmrrc/server.py")

        self.assertIn("handleAuthExpired", controls)
        self.assertIn("ev.code === 4001", controls)
        self.assertIn("TX Opus Worker auth expired", controls)
        self.assertIn("type: 'authExpired'", worker)
        self.assertIn("e.code === 4001", worker)
        self.assertIn("_AUTH_ASSET_EXTS", server)
        self.assertIn("path.endswith(_AUTH_ASSET_EXTS)", server)

    def test_tx_uplink_pre_mod_capture_writes_raw_and_timed_wav(self):
        server = read("sunmrrc/server.py")

        self.assertIn("TX_UPLINK_CAPTURE_DIR", server)
        self.assertIn("tx_uplink_pre_mod_latest_raw.wav", server)
        self.assertIn("tx_uplink_pre_mod_latest_timed.wav", server)
        self.assertIn("tx_uplink_pre_mod_latest.csv", server)
        self.assertIn("gap_samples", server)

    def test_iphone_auto_eq_uses_comfortable_preset_not_strong(self):
        controls = read("sunmrrc/static/controls.js")
        txeq = read("sunmrrc/static/modules/tx_audio_eq.js")

        iphone_branch = re.search(
            r"if \(isIPhone\) \{(?P<body>.*?)\} else if \(isMobile\)",
            controls,
            re.S,
        )
        self.assertIsNotNone(iphone_branch)
        self.assertNotIn("setTX_EQ_Preset('STRONG')", iphone_branch.group("body"))
        self.assertIn("'COMFORT'", txeq)

    def test_entrypoint_busts_cached_audio_chain_scripts(self):
        index = read("sunmrrc/static/index.html")

        self.assertIn("/controls.js?v=5.8.0", index)
        self.assertIn("/mobile.js?v=5.8.0", index)

    def test_restart_background_detaches_stdin_and_unbuffers_logs(self):
        restart = read("sunmrrc/restart.sh")

        self.assertIn("nohup python3 -u server.py", restart)
        self.assertIn("< /dev/null", restart)
        self.assertIn("disown", restart)


if __name__ == "__main__":
    unittest.main()

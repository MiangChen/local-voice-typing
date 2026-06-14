#!/usr/bin/env python3
"""
常驻语音听写守护进程 (本地 GPU / Whisper large-v3)。跨平台：Linux + macOS。
- 加载一次模型常驻显存/内存。
- 收到 SIGUSR1：第一次 -> 开始录音；第二次 -> 停止 + 识别 + 写入剪贴板。
- 识别结果自动进系统剪贴板（Ctrl/Cmd+V 粘贴），并记入历史。
- 自带本地 HTML 仪表盘：http://127.0.0.1:8765 （当前设备/状态/历史100条/一键复制）。
配置见下方 CONFIG。
"""
import os, sys, signal, subprocess, time, wave, json, shutil, tempfile, threading
import http.server
import numpy as np

# ----------------- CONFIG -----------------
INPUT_DEVICE = "auto"           # "auto"=跟随系统默认输入设备；也可填设备名或关键字(如 "UGREEN")来锁定
MODEL       = "openai/whisper-large-v3"
LANGUAGE    = "chinese"         # 强制语种；想自动检测改成 None
SILENCE_RMS = 0.010             # 录音整体音量(RMS)低于此值视为没说话，跳过(避免静音幻觉)；漏字调小、误触发调大
HALLUCINATIONS = ["请不吝点赞", "打赏支持明镜", "谢谢观看", "谢谢大家", "字幕", "订阅", "明镜与点点"]
RATE        = 16000
DEBUG       = True              # True: 每次录音存 ~/stt/debug/last_rec.{wav,mp3} 并记录音量
HTTP_HOST   = "127.0.0.1"
HTTP_PORT   = 8765
HISTORY_MAX = 100
# ------------------------------------------

IS_MAC   = sys.platform == "darwin"
HOME     = os.path.expanduser("~")
STT_DIR  = os.path.join(HOME, "stt")
RUN_DIR  = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
WAV_PATH = os.path.join(RUN_DIR, "stt_rec.wav")
PID_PATH = os.path.join(RUN_DIR, "stt_daemon.pid")
LOG_PATH = os.path.join(STT_DIR, "daemon.log")
DEBUG_DIR = os.path.join(STT_DIR, "debug")
HISTORY_PATH = os.path.join(STT_DIR, "history.json")

os.makedirs(STT_DIR, exist_ok=True)

def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ======================= 平台层 =======================
def _check(args, timeout=5):
    return subprocess.check_output(args, text=True, timeout=timeout,
                                   env={**os.environ, "LC_ALL": "C"})

def notify(title, body="", urgency="normal"):
    try:
        if IS_MAC:
            t = title.replace('"', "'"); b = body.replace('"', "'")
            subprocess.Popen(["osascript", "-e",
                              'display notification "%s" with title "%s"' % (b, t)])
        else:
            subprocess.Popen(["notify-send", "-a", "语音听写", "-u", urgency, title, body])
    except Exception:
        pass

def clipboard_copy(text):
    data = text.encode("utf-8")
    try:
        if IS_MAC:
            cmd = ["pbcopy"]
        elif shutil.which("wl-copy") and os.environ.get("WAYLAND_DISPLAY"):
            cmd = ["wl-copy"]
        elif shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard"]
        elif shutil.which("xsel"):
            cmd = ["xsel", "--clipboard", "--input"]
        else:
            log("no clipboard tool found"); return
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        p.communicate(data)
    except Exception as e:
        log("clipboard copy failed:", repr(e))

def get_input_device():
    """返回 (recorder_id, 友好名称)。recorder_id 传给录音命令。"""
    if IS_MAC:
        dev = ":default" if INPUT_DEVICE == "auto" else (
            INPUT_DEVICE if INPUT_DEVICE.startswith(":") else ":" + INPUT_DEVICE)
        return dev, ("系统默认输入" if dev == ":default" else INPUT_DEVICE)
    # ---- Linux (PulseAudio/PipeWire via pactl) ----
    def desc(src):
        try:
            out = _check(["pactl", "list", "sources"])
        except Exception:
            return src
        name = None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Name:"):
                name = s[5:].strip()
            elif s.startswith("Description:") and name == src:
                return s.split(":", 1)[1].strip()
        return src

    def real_inputs():
        rows = []
        try:
            for line in _check(["pactl", "list", "short", "sources"]).splitlines():
                c = line.split("\t")
                if len(c) >= 2 and c[1].startswith("alsa_input") and ".monitor" not in c[1]:
                    rows.append(c[1])
        except Exception:
            pass
        return rows

    if INPUT_DEVICE != "auto":
        for name in real_inputs():
            if INPUT_DEVICE.lower() in name.lower():
                return name, desc(name)
        return INPUT_DEVICE, INPUT_DEVICE
    # auto: 系统默认源，但避开 .monitor 回采
    src = ""
    try:
        src = _check(["pactl", "get-default-source"]).strip()
    except Exception:
        pass
    if not src or ".monitor" in src:
        ins = real_inputs()
        src = ins[0] if ins else src
    return (src or None), (desc(src) if src else "(无输入设备)")

def record_command(device, wav):
    if IS_MAC:
        # ffmpeg avfoundation。device 形如 ":default" / ":0" / ":设备名"。未在本机实测，必要时调 INPUT_DEVICE。
        return ["ffmpeg", "-y", "-loglevel", "error", "-f", "avfoundation",
                "-i", device, "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", wav]
    cmd = ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", wav]
    if device:
        cmd[1:1] = ["--target", device]
    return cmd
# =====================================================

# ---- 录音控制（写 WAV，SIGTERM 时正确收尾） ----
_rec_proc = None

def start_recording():
    global _rec_proc
    try:
        os.remove(WAV_PATH)
    except FileNotFoundError:
        pass
    dev_id, dev_name = get_input_device()
    STATE_INFO["device"] = dev_name
    cmd = record_command(dev_id, WAV_PATH)
    _rec_err = open(os.path.join(STT_DIR, "record.err"), "w")
    _rec_proc = subprocess.Popen(cmd, stderr=_rec_err)
    log("recording started pid", _rec_proc.pid, "| device:", dev_name, "| id:", dev_id)

def stop_recording():
    global _rec_proc
    if _rec_proc is None:
        return None
    _rec_proc.terminate()
    try:
        _rec_proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        _rec_proc.kill()
    _rec_proc = None
    if not os.path.exists(WAV_PATH):
        return None
    with wave.open(WAV_PATH, "rb") as w:
        sr = w.getframerate()
        frames = w.readframes(w.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    log("recorded %.2fs @%dHz  rms=%.4f peak=%.4f (gate=%.4f)" %
        (len(audio) / sr, sr, rms, peak, SILENCE_RMS))
    if DEBUG:
        try:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            wavp = os.path.join(DEBUG_DIR, "last_rec.wav")
            shutil.copy(WAV_PATH, wavp)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wavp,
                            os.path.join(DEBUG_DIR, "last_rec.mp3")], timeout=15)
        except Exception as e:
            log("debug save failed:", repr(e))
    return audio

# ---- 历史记录 ----
_history = []                 # 最新在前
_hist_lock = threading.Lock()

def load_history():
    global _history
    try:
        with open(HISTORY_PATH) as f:
            _history = json.load(f)[:HISTORY_MAX]
    except Exception:
        _history = []

def add_history(text, device, rms, dur):
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text,
             "device": device, "rms": round(rms, 4), "dur": round(dur, 2)}
    with _hist_lock:
        _history.insert(0, entry)
        del _history[HISTORY_MAX:]
        try:
            with open(HISTORY_PATH, "w") as f:
                json.dump(_history, f, ensure_ascii=False, indent=1)
        except Exception as e:
            log("history save failed:", repr(e))

# ---- 仪表盘状态 ----
STATE_INFO = {"state": "idle", "device": "(检测中)"}

# ---- 模型 ----
def load_model():
    import torch
    from transformers import pipeline
    log("loading model", MODEL, "...")
    asr = pipeline("automatic-speech-recognition", model=MODEL,
                   torch_dtype=torch.float16, device="cuda:0",
                   chunk_length_s=30, batch_size=8)
    warm = np.zeros(RATE, dtype=np.float32)
    asr({"array": warm, "sampling_rate": RATE},
        generate_kwargs={"language": LANGUAGE, "task": "transcribe"} if LANGUAGE else {"task": "transcribe"})
    log("model ready (warmed up)")
    return asr

def transcribe(asr, audio):
    gk = {"task": "transcribe", "repetition_penalty": 1.3, "no_repeat_ngram_size": 4}
    if LANGUAGE:
        gk["language"] = LANGUAGE
    return asr({"array": audio, "sampling_rate": RATE}, generate_kwargs=gk)["text"].strip()

# ===================== HTML 仪表盘 =====================
PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>语音听写 · 仪表盘</title><style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif;
 background:#0f1115;color:#e6e6e6}
header{position:sticky;top:0;background:#161a22;border-bottom:1px solid #262c38;
 padding:14px 20px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}
.kv{font-size:13px;color:#9aa4b2}.kv b{color:#e6e6e6;font-weight:600}
.badge{padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.badge.idle{background:#243042;color:#7fa8d8}
.badge.recording{background:#3a1d22;color:#ff7b86}
.badge.transcribing{background:#3a341d;color:#f0c85a}
main{max-width:900px;margin:0 auto;padding:16px 20px 60px}
.item{background:#161a22;border:1px solid #262c38;border-radius:10px;padding:12px 14px;margin:10px 0;
 display:grid;grid-template-columns:1fr auto;gap:6px 12px;align-items:start}
.meta{grid-column:1;display:flex;gap:12px;font-size:12px;color:#7d8796}
.txt{grid-column:1;white-space:pre-wrap;word-break:break-word;font-size:15px}
button{grid-row:1/3;grid-column:2;align-self:center;background:#2a6df4;color:#fff;border:0;
 border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer;white-space:nowrap}
button:hover{background:#1f5ad6}
.empty{color:#7d8796;text-align:center;padding:40px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:#7fa8d8}
.recording .dot{background:#ff7b86}.transcribing .dot{background:#f0c85a}
</style></head><body>
<header>
 <h1>🎙️ 语音听写</h1>
 <div class="kv">输入设备：<b id="dev">…</b></div>
 <div class="kv">状态：<span id="state" class="badge idle"><span class="dot"></span>…</span></div>
 <div class="kv" id="count"></div>
</header>
<main><div id="hist"></div></main>
<script>
const NAMES={idle:"空闲",recording:"录音中",transcribing:"识别中"};
async function refresh(){
 let d; try{d=await(await fetch('/api/state')).json()}catch(e){return}
 document.getElementById('dev').textContent=d.device||'(未知)';
 const st=document.getElementById('state');
 st.className='badge '+d.state;
 st.innerHTML='<span class="dot"></span>'+(NAMES[d.state]||d.state);
 document.getElementById('count').textContent='历史 '+d.history.length+' 条';
 const box=document.getElementById('hist');
 if(!d.history.length){box.innerHTML='<div class="empty">还没有识别记录。按快捷键说句话试试。</div>';return}
 box.innerHTML='';
 for(const h of d.history){
  const div=document.createElement('div');div.className='item';
  const meta=document.createElement('div');meta.className='meta';
  meta.innerHTML='<span>'+h.time+'</span>'+(h.device?'<span>'+h.device+'</span>':'')
    +'<span>'+(h.dur||0)+'s · rms '+(h.rms||0)+'</span>';
  const txt=document.createElement('div');txt.className='txt';txt.textContent=h.text;
  const b=document.createElement('button');b.textContent='复制';
  b.onclick=()=>copy(h.text,b);
  div.append(meta,txt,b);box.appendChild(div);
 }
}
function copy(t,btn){
 const ok=()=>{const o=btn.textContent;btn.textContent='✓ 已复制';setTimeout(()=>btn.textContent=o,1200)};
 if(navigator.clipboard&&navigator.clipboard.writeText)
  navigator.clipboard.writeText(t).then(ok).catch(()=>fb(t,ok));
 else fb(t,ok);
}
function fb(t,ok){const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);
 a.select();try{document.execCommand('copy')}catch(e){}a.remove();ok()}
refresh();setInterval(refresh,2000);
</script></body></html>"""

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass
    def do_GET(self):
        if self.path.startswith("/api/state"):
            with _hist_lock:
                hist = list(_history)
            payload = {"state": STATE_INFO["state"], "device": STATE_INFO["device"],
                       "history": hist}
            self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")

def start_dashboard():
    try:
        srv = http.server.ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), _Handler)
    except OSError as e:
        log("dashboard bind failed (%s); maybe already running" % e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # 启动时先把当前设备名填进去
    try:
        STATE_INFO["device"] = get_input_device()[1]
    except Exception:
        pass
    log("dashboard at http://%s:%d" % (HTTP_HOST, HTTP_PORT))
# =====================================================

# ---- 主状态机 ----
_toggle = False
def on_sigusr1(signum, frame):
    global _toggle
    _toggle = True

def main():
    global _toggle
    # 单实例保护
    if os.path.exists(PID_PATH):
        try:
            old = int(open(PID_PATH).read().strip())
            os.kill(old, 0)
            log("another daemon (pid %d) already running, exiting" % old)
            return
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    open(PID_PATH, "w").write(str(os.getpid()))
    signal.signal(signal.SIGUSR1, on_sigusr1)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    load_history()
    start_dashboard()
    asr = load_model()
    notify("语音听写就绪", "仪表盘 http://%s:%d" % (HTTP_HOST, HTTP_PORT), "low")
    while True:
        if _toggle:
            _toggle = False
            if STATE_INFO["state"] == "idle":
                start_recording()
                STATE_INFO["state"] = "recording"
                notify("🎙️ 录音中…", "再按一次快捷键停止并识别", "low")
            elif STATE_INFO["state"] == "recording":
                STATE_INFO["state"] = "transcribing"
                notify("✍️ 识别中…", "", "low")
                audio = stop_recording()
                dur = len(audio) / RATE if audio is not None else 0.0
                rms = float(np.sqrt(np.mean(audio ** 2))) if audio is not None and len(audio) else 0.0
                if audio is None or len(audio) < RATE * 0.3:
                    log("recording empty/too short")
                    notify("⚠️ 录音太短", "没听清，再试一次", "normal")
                elif rms < SILENCE_RMS:
                    log("too quiet, rms=%.4f < %.4f, skip" % (rms, SILENCE_RMS))
                    notify("🔇 没听到声音", "环境太安静或没说话 (rms=%.3f)" % rms, "normal")
                else:
                    try:
                        text = transcribe(asr, audio)
                    except Exception as e:
                        log("ERROR", repr(e))
                        notify("❌ 识别出错", str(e)[:120], "critical")
                        STATE_INFO["state"] = "idle"
                        continue
                    hit = next((h for h in HALLUCINATIONS if h in text), None)
                    if text and hit and len(text) < 40:
                        log("dropped hallucination:", text)
                        notify("🔇 没听到有效语音", "(疑似静音幻觉，已忽略)", "normal")
                    elif text:
                        clipboard_copy(text)
                        add_history(text, STATE_INFO["device"], rms, dur)
                        log("=>", text)
                        notify("✅ 已复制到剪贴板", text[:80], "normal")
                    else:
                        notify("⚠️ 没识别到文字", "", "normal")
                STATE_INFO["state"] = "idle"
        time.sleep(0.05)

if __name__ == "__main__":
    main()

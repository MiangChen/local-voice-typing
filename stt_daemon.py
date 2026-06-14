#!/usr/bin/env python3
"""
常驻语音听写守护进程 (本地 GPU / Whisper large-v3)。跨平台：Linux + macOS。

- 模型常驻显存/内存。按快捷键触发(经 FIFO)：第一次开始录音，第二次停止+识别。
- 识别结果自动进系统剪贴板（Ctrl/Cmd+V 粘贴），并记入历史。
- 顶栏/菜单栏【托盘图标】实时显示状态：
    灰=空闲  蓝=聆听中  橙=识别中  绿✓=成功  红!=失败
  点击图标弹出菜单可看“上次结果/报错详情”、一键复制、打开仪表盘。
- 本地 HTML 仪表盘：http://127.0.0.1:8765 （设备/状态/历史100条/一键复制）。
配置见下方 CONFIG。
"""
import os, sys, signal, subprocess, time, wave, json, shutil, tempfile, threading, queue
import http.server
import numpy as np

# ----------------- CONFIG -----------------
INPUT_DEVICE = "auto"           # "auto"=跟随系统默认输入；也可填设备名或关键字锁定
MODEL       = "openai/whisper-large-v3"
LANGUAGE    = "chinese"
SILENCE_RMS = 0.010
HALLUCINATIONS = ["请不吝点赞", "打赏支持明镜", "谢谢观看", "谢谢大家", "字幕", "订阅", "明镜与点点"]
RATE        = 16000
DEBUG       = True
USE_NOTIFICATIONS = False       # 有了托盘图标后默认关闭系统弹窗通知；托盘不可用时会自动回退开启
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
FIFO_PATH = os.path.join(RUN_DIR, "stt_toggle.fifo")
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
    if not USE_NOTIFICATIONS:
        return
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
    if IS_MAC:
        cmd = ["pbcopy"]
    elif shutil.which("wl-copy"):
        cmd = ["wl-copy"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard"]
    elif shutil.which("xsel"):
        cmd = ["xsel", "--clipboard", "--input"]
    else:
        log("no clipboard tool found"); return False
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        p.communicate(data)
        return p.returncode == 0
    except Exception as e:
        log("clipboard copy failed:", repr(e)); return False

def open_url(url):
    try:
        subprocess.Popen((["open", url] if IS_MAC else ["xdg-open", url]),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("open url failed:", repr(e))

def get_input_device():
    """返回 (recorder_id, 友好名称)。"""
    if IS_MAC:
        dev = ":default" if INPUT_DEVICE == "auto" else (
            INPUT_DEVICE if INPUT_DEVICE.startswith(":") else ":" + INPUT_DEVICE)
        return dev, ("系统默认输入" if dev == ":default" else INPUT_DEVICE)
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
        return ["ffmpeg", "-y", "-loglevel", "error", "-f", "avfoundation",
                "-i", device, "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", wav]
    cmd = ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", wav]
    if device:
        cmd[1:1] = ["--target", device]
    return cmd
# =====================================================

# ---- 录音 ----
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
    log("recording started pid", _rec_proc.pid, "| device:", dev_name)

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

# ---- 历史 ----
_history = []
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

STATE_INFO = {"state": "idle", "device": "(检测中)"}

# ===================== 托盘图标 =====================
# 状态：loading/idle/recording/transcribing/success/error
TRAY = {"state": "loading", "detail": "模型加载中…", "last_text": "", "last_error": ""}
_icon = [None]
COLORS = {
    "loading": (120, 130, 145), "idle": (110, 118, 130),
    "recording": (42, 109, 244), "transcribing": (240, 160, 0),
    "success": (31, 170, 85), "error": (226, 59, 59),
}
TITLES = {
    "loading": "语音听写 · 模型加载中", "idle": "语音听写 · 就绪",
    "recording": "🎙️ 聆听中", "transcribing": "✍️ 识别中",
    "success": "✓ 识别成功", "error": "✗ 识别失败",
}

def make_icon(state):
    from PIL import Image, ImageDraw
    sz = 64
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = COLORS.get(state, COLORS["idle"])
    d.ellipse([2, 2, sz - 3, sz - 3], fill=col + (255,))
    W = (255, 255, 255, 255)
    if state == "success":
        d.line([(17, 33), (28, 45), (48, 20)], fill=W, width=7, joint="curve")
    elif state == "error":
        d.rounded_rectangle([29, 15, 35, 39], radius=3, fill=W)
        d.ellipse([28, 44, 36, 52], fill=W)
    else:
        # 麦克风
        d.rounded_rectangle([26, 14, 38, 37], radius=6, fill=W)
        d.arc([21, 18, 43, 43], start=20, end=160, fill=W, width=4)
        d.line([(32, 43), (32, 50)], fill=W, width=4)
        d.line([(25, 50), (39, 50)], fill=W, width=4)
    return img

def set_tray(state, detail="", last_text=None, last_error=None):
    TRAY["state"] = state
    TRAY["detail"] = detail
    if last_text is not None:
        TRAY["last_text"] = last_text
    if last_error is not None:
        TRAY["last_error"] = last_error
    ic = _icon[0]
    if ic is not None:
        try:
            ic.icon = make_icon(state)
            ic.title = (detail or TITLES.get(state, "语音听写"))[:120]
            ic.update_menu()
        except Exception as e:
            log("tray update failed:", repr(e))

def _menu_header(item):
    s = TRAY["state"]
    tag = {"success": "✓ ", "error": "✗ ", "recording": "🎙️ ",
           "transcribing": "✍️ "}.get(s, "")
    txt = TRAY["detail"] or TITLES.get(s, "语音听写")
    return (tag + txt)[:110]

def build_menu():
    import pystray
    return pystray.Menu(
        pystray.MenuItem(_menu_header, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("复制上次结果",
                         lambda i: clipboard_copy(TRAY["last_text"]),
                         enabled=lambda i: bool(TRAY["last_text"])),
        pystray.MenuItem("打开仪表盘", lambda i: open_url("http://%s:%d" % (HTTP_HOST, HTTP_PORT))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda i: _quit()),
    )

def _quit():
    try:
        if _rec_proc:
            _rec_proc.terminate()
    except Exception:
        pass
    if _icon[0]:
        try:
            _icon[0].stop()
        except Exception:
            pass
    os._exit(0)

def run_tray():
    """主线程运行托盘 GUI 循环。失败则回退：开启通知并空转(worker 仍工作)。"""
    global USE_NOTIFICATIONS
    try:
        import pystray
        icon = pystray.Icon("voice-typing", make_icon(TRAY["state"]),
                            TITLES.get(TRAY["state"], "语音听写"), menu=build_menu())
        _icon[0] = icon
        set_tray(TRAY["state"], TRAY["detail"])
        icon.run()
    except Exception as e:
        log("tray unavailable, fallback to notifications:", repr(e))
        USE_NOTIFICATIONS = True
        _icon[0] = None
        while True:
            time.sleep(3600)
# =====================================================

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
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif;background:#0f1115;color:#e6e6e6}
header{position:sticky;top:0;background:#161a22;border-bottom:1px solid #262c38;padding:14px 20px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}
.kv{font-size:13px;color:#9aa4b2}.kv b{color:#e6e6e6;font-weight:600}
.badge{padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.badge.idle{background:#243042;color:#7fa8d8}.badge.recording{background:#3a1d22;color:#ff7b86}.badge.transcribing{background:#3a341d;color:#f0c85a}
main{max-width:900px;margin:0 auto;padding:16px 20px 60px}
.item{background:#161a22;border:1px solid #262c38;border-radius:10px;padding:12px 14px;margin:10px 0;display:grid;grid-template-columns:1fr auto;gap:6px 12px;align-items:start}
.meta{grid-column:1;display:flex;gap:12px;font-size:12px;color:#7d8796}
.txt{grid-column:1;white-space:pre-wrap;word-break:break-word;font-size:15px}
button{grid-row:1/3;grid-column:2;align-self:center;background:#2a6df4;color:#fff;border:0;border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer;white-space:nowrap}
button:hover{background:#1f5ad6}.empty{color:#7d8796;text-align:center;padding:40px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:#7fa8d8}
.recording .dot{background:#ff7b86}.transcribing .dot{background:#f0c85a}
</style></head><body>
<header><h1>🎙️ 语音听写</h1>
 <div class="kv">输入设备：<b id="dev">…</b></div>
 <div class="kv">状态：<span id="state" class="badge idle"><span class="dot"></span>…</span></div>
 <div class="kv" id="count"></div></header>
<main><div id="hist"></div></main>
<script>
const NAMES={idle:"空闲",recording:"录音中",transcribing:"识别中"};
async function refresh(){let d;try{d=await(await fetch('/api/state')).json()}catch(e){return}
 document.getElementById('dev').textContent=d.device||'(未知)';
 const st=document.getElementById('state');st.className='badge '+d.state;
 st.innerHTML='<span class="dot"></span>'+(NAMES[d.state]||d.state);
 document.getElementById('count').textContent='历史 '+d.history.length+' 条';
 const box=document.getElementById('hist');
 if(!d.history.length){box.innerHTML='<div class="empty">还没有识别记录。按快捷键说句话试试。</div>';return}
 box.innerHTML='';
 for(const h of d.history){const div=document.createElement('div');div.className='item';
  const meta=document.createElement('div');meta.className='meta';
  meta.innerHTML='<span>'+h.time+'</span>'+(h.device?'<span>'+h.device+'</span>':'')+'<span>'+(h.dur||0)+'s · rms '+(h.rms||0)+'</span>';
  const txt=document.createElement('div');txt.className='txt';txt.textContent=h.text;
  const b=document.createElement('button');b.textContent='复制';b.onclick=()=>copy(h.text,b);
  div.append(meta,txt,b);box.appendChild(div);}}
function copy(t,btn){const ok=()=>{const o=btn.textContent;btn.textContent='✓ 已复制';setTimeout(()=>btn.textContent=o,1200)};
 if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(t).then(ok).catch(()=>fb(t,ok));else fb(t,ok);}
function fb(t,ok){const a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();try{document.execCommand('copy')}catch(e){}a.remove();ok()}
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
            payload = {"state": STATE_INFO["state"], "device": STATE_INFO["device"], "history": hist}
            self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")

def start_dashboard():
    try:
        srv = http.server.ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), _Handler)
    except OSError as e:
        log("dashboard bind failed (%s)" % e); return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        STATE_INFO["device"] = get_input_device()[1]
    except Exception:
        pass
    log("dashboard at http://%s:%d" % (HTTP_HOST, HTTP_PORT))
# =====================================================

# ---- 触发：FIFO 命名管道（与 GUI 主循环解耦） ----
_toggle_q = queue.Queue()

def fifo_listener():
    try:
        if not os.path.exists(FIFO_PATH):
            os.mkfifo(FIFO_PATH)
    except FileExistsError:
        pass
    except Exception as e:
        log("mkfifo failed:", repr(e)); return
    while True:
        try:
            with open(FIFO_PATH) as f:
                for _ in f:
                    _toggle_q.put(1)
        except Exception as e:
            log("fifo error:", repr(e)); time.sleep(0.5)

# ---- 工作线程：模型 + 录音/识别状态机 ----
def worker():
    set_tray("loading", "模型加载中…（首次需下载~3GB）")
    asr = load_model()
    set_tray("idle", "就绪 · 按快捷键开始说话")
    state = "idle"
    while True:
        _toggle_q.get()
        if state == "idle":
            start_recording()
            state = "recording"
            STATE_INFO["state"] = "recording"
            set_tray("recording", "🎙️ 聆听中…再按一次停止")
            notify("🎙️ 录音中…", "再按一次快捷键停止并识别", "low")
        elif state == "recording":
            state = "idle"
            STATE_INFO["state"] = "transcribing"
            set_tray("transcribing", "✍️ 识别中…")
            audio = stop_recording()
            dur = len(audio) / RATE if audio is not None else 0.0
            rms = float(np.sqrt(np.mean(audio ** 2))) if audio is not None and len(audio) else 0.0
            STATE_INFO["state"] = "idle"
            if audio is None or len(audio) < RATE * 0.3:
                msg = "录音太短，没听清，再试一次"
                log("fail:", msg)
                set_tray("error", "✗ " + msg, last_error=msg)
                notify("⚠️ 录音太短", msg, "normal")
            elif rms < SILENCE_RMS:
                msg = "没听到声音（太安静或没说话，rms=%.3f）" % rms
                log("fail:", msg)
                set_tray("error", "✗ " + msg, last_error=msg)
                notify("🔇 没听到声音", msg, "normal")
            else:
                try:
                    text = transcribe(asr, audio)
                except Exception as e:
                    msg = "识别出错：%s" % (str(e)[:140])
                    log("ERROR", repr(e))
                    set_tray("error", "✗ " + msg, last_error=msg)
                    notify("❌ 识别出错", str(e)[:120], "critical")
                    continue
                hit = next((h for h in HALLUCINATIONS if h in text), None)
                if text and hit and len(text) < 40:
                    msg = "没听到有效语音（疑似静音幻觉，已忽略）"
                    log("dropped hallucination:", text)
                    set_tray("error", "✗ " + msg, last_error=msg)
                    notify("🔇 没听到有效语音", msg, "normal")
                elif text:
                    ok = clipboard_copy(text)
                    add_history(text, STATE_INFO["device"], rms, dur)
                    log("=>", text)
                    detail = ("✓ 已复制到剪贴板：" if ok else "✓ 已识别(复制失败)：") + text
                    set_tray("success", detail, last_text=text)
                    notify("✅ 已复制到剪贴板", text[:80], "normal")
                else:
                    msg = "没识别到文字"
                    set_tray("error", "✗ " + msg, last_error=msg)
                    notify("⚠️ 没识别到文字", "", "normal")

# ---- 主入口 ----
def main():
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
    # SIGUSR1 作为后备触发（FIFO 为主）
    signal.signal(signal.SIGUSR1, lambda *a: _toggle_q.put(1))
    signal.signal(signal.SIGTERM, lambda *a: _quit())
    load_history()
    start_dashboard()
    threading.Thread(target=fifo_listener, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    run_tray()   # 阻塞在主线程

if __name__ == "__main__":
    main()

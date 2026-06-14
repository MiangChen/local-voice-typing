#!/usr/bin/env bash
# 切换录音：第一次按=开始，第二次按=停止并识别。
# 若守护进程没在跑，则先启动它（首次会加载模型，需等几秒）。
PID_FILE="/run/user/$(id -u)/stt_daemon.pid"
DAEMON="$HOME/stt/stt_daemon.py"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill -USR1 "$(cat "$PID_FILE")"
else
    notify-send -a "语音听写" "启动中…" "首次加载模型，请等待“就绪”提示后再按" 2>/dev/null
    nohup python3 "$DAEMON" >>"$HOME/stt/daemon.log" 2>&1 &
fi

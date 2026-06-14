#!/usr/bin/env bash
# 切换录音：第一次按=开始，第二次按=停止并识别（通过 FIFO 通知常驻守护进程）。
# 若守护进程没在跑，则先启动它（首次会加载模型，需等几秒到“就绪”）。
RUN_DIR="${XDG_RUNTIME_DIR:-/tmp}"
FIFO="$RUN_DIR/stt_toggle.fifo"
PID_FILE="$RUN_DIR/stt_daemon.pid"
DAEMON="$HOME/stt/stt_daemon.py"

if [ -p "$FIFO" ] && [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo > "$FIFO"
else
    nohup python3 "$DAEMON" >>"$HOME/stt/daemon.log" 2>&1 &
fi

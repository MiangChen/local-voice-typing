#!/usr/bin/env bash
# 一键安装（Linux / GNOME）：装依赖、设开机自启、绑全局快捷键。
# macOS 用户请参考 README，本脚本仅适用于 Linux。
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[1/4] 系统依赖 (需 sudo)…"
sudo apt-get update -qq
# wl-clipboard 剪贴板 / ffmpeg+pipewire 录音 / 后三者=顶栏托盘图标(AppIndicator)
sudo apt-get install -y wl-clipboard libnotify-bin ffmpeg pipewire-bin \
     python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1

echo "[2/4] Python 依赖…"
PIP="pip install"; $PIP transformers pystray pillow 2>/dev/null || \
  pip install --break-system-packages transformers pystray pillow
echo "  注意：GPU 用户请自行安装对应 CUDA 版本的 torch（见 https://pytorch.org）。"
echo "  GNOME 用户需启用 AppIndicator 扩展(Ubuntu 默认已启用)才能看到顶栏图标。"

echo "[3/4] 开机自启 (~/.config/autostart)…"
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/stt-daemon.desktop <<EOF
[Desktop Entry]
Type=Application
Name=语音听写守护进程
Exec=python3 $DIR/stt_daemon.py
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF

echo "[4/4] 绑定全局快捷键 F10 (GNOME)…"
P=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/stt/
EXIST=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)
if ! echo "$EXIST" | grep -q "custom-keybindings/stt/"; then
  NEW=$(echo "$EXIST" | sed "s/]$/, '$P']/; s/^@as \[\]$/['$P']/")
  gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$NEW"
fi
S="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$P"
gsettings set "$S" name "语音听写 开始/停止"
gsettings set "$S" command "$DIR/stt-toggle.sh"
gsettings set "$S" binding "F10"

chmod +x "$DIR/stt-toggle.sh"
echo "✅ 完成。运行 'python3 $DIR/stt_daemon.py' 启动；之后按 F10 听写，仪表盘 http://127.0.0.1:8765"

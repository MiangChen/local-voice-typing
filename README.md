# 🎙️ 本地语音听写 (Local Voice Dictation)

按一个快捷键就开始说话，松手后文字**自动进剪贴板**，任何地方 `Ctrl/Cmd+V` 直接粘贴。
完全**本地离线**运行，基于 OpenAI Whisper，识别在你自己的电脑上完成，**不联网、不上传任何音频**。

> 截图占位（稍后插入）
>
> ![仪表盘截图](docs/dashboard.png)
> ![使用演示](docs/demo.gif)

---

## ✨ 它能做什么

- **按键即说，说完即粘**：一个全局快捷键开始/停止；识别结果自动写入系统剪贴板。
- **本地 GPU 加速**：用 Whisper `large-v3`，中英文都很准；有 NVIDIA 显卡时近实时。
- **常驻不卡顿**：模型常驻显存，每次识别即时响应，无需反复加载。
- **网页仪表盘**：`http://127.0.0.1:8765` 实时查看当前麦克风、录音状态、历史 100 条，每条一键复制。
- **跟随系统麦克风**：自动使用系统默认输入设备，换设备无需改配置。
- **防误触**：静音/环境噪声自动跳过，不会把空录音的“幻觉”塞进剪贴板。

## 🎯 适合的任务场景

- 📝 **写作 / 记笔记**：口述初稿、灵感速记，比打字快。
- 💬 **聊天 / 邮件 / 评论**：说完直接粘贴到任意输入框。
- 👨‍💻 **写代码注释 / commit / 文档**：解放双手描述思路。
- 🌐 **配合翻译 / 大模型**：先把口语转成文字，再丢给任意工具。
- ♿ **无障碍 / 减少手部负担**：长时间打字不便时的替代输入。
- 🎧 **长段口述**：会议要点、想法整理，自动分段转写。

> 不适合：需要逐字稿、时间轴字幕、或多人会议分离说话人的专业场景（这类建议用专门的转录软件）。

## 💻 支持的设备与系统

| 系统 | 状态 | 录音 / 剪贴板 / 通知 |
|------|------|----------------------|
| **Linux**（已在 Ubuntu + GNOME/Wayland 实测） | ✅ 完整支持 | `pw-record` / `wl-copy`(或 xclip) / `notify-send` |
| **macOS** | 🧪 已实现，待联调 | `ffmpeg(avfoundation)` / `pbcopy` / `osascript` |

**算力**：

- 🟢 **NVIDIA GPU（推荐）**：`large-v3` 近实时（在 RTX 5090 上亚秒级）。新架构显卡（如 50 系/Blackwell）请确保 PyTorch 为对应 CUDA 版本。
- 🟡 **纯 CPU 也能跑**：把模型换成更小的（`medium` / `small`），速度可接受。
- 🎤 **麦克风**：任意 USB / 内置麦克风，跟随系统默认输入设备。

## 🚀 快速开始（Linux）

```bash
# 1. 依赖（Ubuntu/Debian）
sudo apt install -y wl-clipboard libnotify-bin ffmpeg pipewire-bin
pip install transformers torch   # GPU 用户请装对应 CUDA 版的 torch

# 2. 启动常驻守护进程（首次会下载模型 ~3GB）
python3 stt_daemon.py

# 3. 绑定一个全局快捷键（GNOME 示例：把 F10 绑到 stt-toggle.sh）
#    设置 → 键盘 → 自定义快捷键 → 命令填 /path/to/stt-toggle.sh
```

用法：按快捷键 → 说话 → 再按一次 → 文字已在剪贴板，`Ctrl+V` 粘贴。
打开 **http://127.0.0.1:8765** 查看设备状态与历史记录。

## ⚙️ 配置

编辑 `stt_daemon.py` 顶部 `CONFIG`：

| 选项 | 说明 |
|------|------|
| `INPUT_DEVICE` | `"auto"` 跟随系统默认；或填麦克风名/关键字锁定某个设备 |
| `MODEL` | Whisper 模型，默认 `openai/whisper-large-v3`；CPU 可换 `medium`/`small` |
| `LANGUAGE` | 强制语种（默认 `chinese`）；设 `None` 自动检测 |
| `SILENCE_RMS` | 静音门限，漏字调小、误触发调大 |
| `HTTP_PORT` | 仪表盘端口，默认 `8765` |

## 📄 许可证

MIT

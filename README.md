# AudioVTTForge

AudioVTTForge 是一个运行在 Windows 上的图形化工具，用于将 WAV/MP3 音频、对应的 WebVTT 字幕和图片合成为 MP4 视频。

## 功能

- 支持批量导入 WAV 和 MP3 音频。
- 自动匹配对应的 WebVTT 字幕文件。
- 支持直接拖入图片；未安装拖放组件时也可以通过文件选择器导入。
- 支持多个音频并行处理。
- 默认使用 AAC 编码输出音频。
- 图片按比例缩放，完整显示图片内容，避免强制拉伸。
- 支持选择视频帧率和图片分辨率。
- 提供每个音频的处理状态、进度和整体进度。
- 支持保留中间分段，便于中断后继续处理。
- 支持对已有分段单独执行合并。
- 自动保存上次使用的音频、图片和处理设置。

## 环境要求

- Windows
- Python 3.11 或更高版本
- FFmpeg 和 FFprobe
- 可选：`tkinterdnd2`，用于启用原生文件拖放
- PyInstaller，用于构建 EXE

默认 FFmpeg 路径由程序配置。若本机路径不同，请在源码中的配置区域修改。

## 运行源码

在项目目录中执行：

```powershell
python .\audio_vtt_to_mp4_gui.py
```

核心处理流程也可以完全脱离 GUI 运行。CLI 使用一个 JSON 文件描述任务：

```json
{
  "audio": ["01.wav"],
  "images": ["cover.jpg"],
  "assignments": [0],
  "output": "result.mp4",
  "subtitle": "burnin",
  "fps": 2,
  "width": 1920,
  "workers": 2
}
```

CLI 命令：

```powershell
python -m audiovttforge doctor
python -m audiovttforge validate .\job.json
python -m audiovttforge plan .\job.json
python -m audiovttforge run .\job.json --keep-work
python -m audiovttforge run .\job.json --workers 1 --resume
python -m audiovttforge merge .\.result_parallel_work .\result.mp4
```

`run` 会将 JSONL 事件写入输出目录的 `.result.events.jsonl`，失败时保留中间工作目录和每个分段的 FFmpeg 日志。`--keep-work` 保留成功任务的中间文件，`--resume` 跳过已经生成的分段。`--workers 1` 适合复现问题，CLI 的标准输出也会逐行输出同样的机器可读事件。

FFmpeg 和 FFprobe 默认使用源码中的 Windows 路径，也可以通过 `AUDIOVTTFORGE_FFMPEG`、`AUDIOVTTFORGE_FFPROBE` 环境变量或 CLI 参数覆盖。

## 构建 EXE

执行：

```powershell
.\build.ps1
```

构建完成后，EXE 位于：

```text
dist\AudioVTTForge.exe
```

## 输入文件命名

音频和字幕建议使用以下命名方式：

```text
01.wav
01.wav.vtt
cover.jpg
```

也支持 MP3：

```text
01.mp3
01.mp3.vtt
```

程序会根据音频文件名查找对应的 `.wav.vtt` 或 `.mp3.vtt` 文件。

## 图片分配规则

默认情况下，图片仍按照音频顺序自动分组分配。

例如有 5 个音频和 2 张图片：

- 第 1、2、3 个音频使用第 1 张图片；
- 第 4、5 个音频使用第 2 张图片。

导入音频和图片后，可以在 GUI 的“音频-图片分配”区域逐条选择图片。每个音频都可以单独指定图片，同一张图片也可以分配给多个音频；选择“自动”可以恢复该音频的默认分配规则。手动分配会自动保存到本地设置，并在下次启动时恢复。

## 输出和中间文件

输出 MP4 会根据音频所在目录和输入文件自动生成。处理过程中会创建并行工作目录，用于保存中间视频分段。

确认合成完成后，程序会清理本次生成的中间文件。如果处理中断，可以使用已有分段执行单独合并，或重新开始处理。

## 架构和测试

`audiovttforge` 包含不依赖 Tkinter 的媒体解析、任务模型、FFmpeg engine 和 CLI。GUI 只负责编辑输入、生成任务快照和消费 engine 事件。

运行 headless 测试：

```powershell
python -m pytest -q
```

## 许可证

如果计划公开发布或再分发本项目，请在发布前添加合适的许可证文件。

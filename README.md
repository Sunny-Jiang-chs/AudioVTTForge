# AudioVTTForge

AudioVTTForge 是一个运行在 Windows 上的本地媒体渲染工具，用于将 WAV/MP3 音频、对应的字幕和图片合成为 MP4 视频。主要交互方式是本地 REST API 与浏览器界面，同时保留 Python/Tk GUI 和 CLI 入口。

## 功能

- 支持批量导入 WAV 和 MP3 音频。
- 自动匹配对应的 WebVTT 或 LRC 字幕文件。
- 支持直接拖入图片；未安装拖放组件时也可以通过文件选择器导入。
- 支持多个音频并行处理。
- 默认使用 AAC 编码输出音频。
- 图片按比例缩放，完整显示图片内容，避免强制拉伸。
- 支持选择视频帧率和图片分辨率。
- 提供每个音频的处理状态、进度和整体进度。
- 支持保留中间分段，便于中断后继续处理。
- 支持对已有分段单独执行合并。
- 自动保存上次使用的音频、图片和处理设置。
- 浏览器端通过 REST API 扫描素材、提交任务、查看进度、取消任务并下载结果。

## 环境要求

- Windows
- Python 3.11 或更高版本
- FFmpeg 和 FFprobe
- 可选：`tkinterdnd2`，用于启用原生文件拖放
- PyInstaller，用于构建 EXE

默认 FFmpeg 路径由程序配置。若本机路径不同，请在源码中的配置区域修改。

## 运行源码

### 浏览器界面（主要入口）

```powershell
python -m audiovttforge.web --port 8765
```

Windows 下也可以直接双击项目根目录的 `start_web.bat`。它会启动本地服务并自动打开浏览器；
服务运行期间请保留弹出的命令窗口，关闭该窗口即可停止服务。

默认只监听 `127.0.0.1`，不会把素材发送到远程服务。
浏览器版的默认流程是输入素材目录路径并点击“扫描目录”。因为服务和浏览器运行在同一台电脑上，
服务可以直接读取这个本地目录；扫描结果会在页面内按数字自然顺序展示音频、图片、VTT/LRC 字幕以及缺失字幕警告。
确认目录内容后，前端会为每段音频显示图片选择器，默认沿用 GUI 的自动分配规则，也可以手动指定某张图片；
同时可以选择 1 到 10 个并行进程。前端只提交路径、图片分配和渲染参数，素材本身不会通过浏览器上传。旧的上传接口仍保留，供其他 API
调用方兼容使用。
烧录字幕时还可以在输出设置中选择字体、字号和颜色；这些选项会随 REST 请求传入渲染引擎。
设置区同时提供实时字幕效果预览，可修改示例文字确认样式；它不会替代最终视频渲染。
输出目录默认使用素材目录，也可以在提交前改为其他本机目录。服务先在任务缓存目录完成渲染，
再校验并原子发布最终 MP4；确认目标文件写入成功后会立即清理该任务的缓存文件。

### Python/Tk GUI

GUI 入口仍可直接运行：

```powershell
python .\audio_vtt_to_mp4_gui.py
```

媒体处理由同一个 `RenderEngine` 执行；日常主要流程建议使用上面的浏览器界面。

核心资源如下：

```text
GET  /api/v1/health
GET  /api/v1/capabilities
POST /api/v1/sources/scan
GET  /api/v1/sources/image?directory={path}&name={filename}
POST /api/v1/uploads
POST /api/v1/jobs
GET  /api/v1/jobs/{id}
GET  /api/v1/jobs/{id}/events?after={seq}
GET  /api/v1/jobs/{id}/download
DELETE /api/v1/jobs/{id}
```

`POST /api/v1/sources/scan` 接收 `{ "path": "D:\\AudioVTT\\episode01" }`，返回目录内按自然顺序整理的
音频、图片和 VTT/LRC 字幕清单。字幕优先按 `音频文件名.vtt`、`音频文件名.lrc`，再按不带音频扩展名的同名文件匹配。随后 `POST /api/v1/jobs` 可以使用 `{ "source_dir": "..." }` 创建任务，返回
`202 Accepted`；前端通过任务资源和事件资源轮询进度。`output_dir` 可指定最终输出目录，省略时本地素材任务默认输出到素材目录；下载资源仍可用于另存副本。
预览图片资源只提供指定素材目录中已扫描到的图片，供网页进行画面与字幕叠加预览。
扫描目录时，默认输出名取完整目录路径中最长的目录名片段并添加 `.mp4`；用户可以在渲染前修改。
渲染开始后可调用 `DELETE /api/v1/jobs/{id}` 取消任务。服务会结束正在运行的 FFmpeg/FFprobe 进程，
清理本次任务目录，并保留原素材目录；网页中的路径、参数和图片分配也会保留，便于调整后重新提交。
浏览器静态文件位于 `audiovttforge/web_static/`，服务端入口位于
`audiovttforge/web.py`。

核心处理流程也可以脱离图形界面运行。CLI 使用一个 JSON 文件描述任务：

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

该脚本构建的是 Python/Tk GUI 的 onedir 目录版，EXE 位于：

```text
dist\AudioVTTForge\AudioVTTForge.exe
```

目录版不需要在启动时解包到 `%TEMP%`，适合日常使用。若确实需要单文件版本，可以执行 `.\build.ps1 -Mode onefile`；单文件版本依赖可写的 `%TEMP%`。构建会先生成到 `dist` 下的临时目录，再重试替换旧目录；若旧版 GUI 正在运行导致替换失败，脚本会保留新构建并报告其路径。

## 输入文件命名

VTT 字幕可使用以下命名方式：

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

程序会优先查找带音频扩展名的字幕名（如 `01.mp3.vtt`），也接受简写的同名字幕（如 `01.vtt`）。

LRC 字幕示例：

```text
01.mp3
01.mp3.lrc
```

LRC 也接受 `01.lrc` 命名。带音频扩展名的 VTT 优先于 LRC；LRC 时间戳会转换成视频字幕时序，供烧录或 MP4 内嵌字幕使用。

## 图片分配规则

默认情况下，图片仍按照音频顺序自动分组分配。

例如有 5 个音频和 2 张图片：

- 第 1、2、3 个音频使用第 1 张图片；
- 第 4、5 个音频使用第 2 张图片。

导入音频和图片后，可以在浏览器界面或 GUI 中逐条指定图片。每个音频都可以单独指定图片，同一张图片也可以分配给多个音频；选择“自动”可以恢复该音频的默认分配规则。GUI 会自动保存手动分配设置，并在下次启动时恢复。

## 输出和中间文件

输出 MP4 会根据音频所在目录和输入文件自动生成。处理过程中会创建并行工作目录，用于保存中间视频分段。

确认合成完成后，程序会清理本次生成的中间文件。如果处理中断，可以使用已有分段执行单独合并，或重新开始处理。

## 架构和测试

`audiovttforge` 包含不依赖 Tkinter 的媒体解析、任务模型、FFmpeg engine、CLI 和 REST API。浏览器界面通过 REST API 管理任务，Tk GUI 作为保留入口直接调用同一处理引擎。

运行 headless 测试：

```powershell
python -m pytest -q
```

## 许可证

如果计划公开发布或再分发本项目，请在发布前添加合适的许可证文件。

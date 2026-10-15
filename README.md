# 织声 · Soundweaver

> 共同工作区：[MediaSuite](../README.md) · 共同开发与前端规范：[AGENTS.md](../AGENTS.md)。

织声（Soundweaver，原 AudioVTTForge）是一个运行在 Windows 上的本地媒体渲染工具，用于将 WAV/MP3 音频、对应的字幕和图片合成为 MP4 视频。应用只有一套桌面界面，由 pywebview / Edge WebView2 提供独立窗口与 Windows 文件选择器，Python 引擎和本地 REST API 执行渲染。CLI 继续可用。

对外名称统一为「织声」，与夜莺播放器配套。项目目录、Python 模块 `audiovttforge`、`AUDIOVTTFORGE_*` 环境变量、REST 服务标识及原有数据目录继续兼容，已有设置和工作区可直接沿用。图标母版与生成提示词见 [品牌资源](assets/branding/README.md)。

## 功能

- 通过 Windows 文件夹选择器导入整个素材目录，批量处理其中的 WAV 和 MP3 音频，无需复制粘贴路径。
- 自动匹配对应的 WebVTT 或 LRC 字幕文件。
- 可在字幕设置中额外生成按音频顺序合并时间戳的 VTT 文件，与 MP4 分开保存。
- 自动扫描同目录图片，也可以多选添加其他目录的图片，并逐个音频调整图片分配。
- 支持多个音频并行处理。
- 默认使用通用的 AAC 编码输出音频，也可以在输出设置中选择 ALAC 无损音频；ALAC 的播放器兼容性低于 AAC。
- 图片按比例缩放，完整显示图片内容，避免强制拉伸。
- 没有图片时仍可生成视频，程序会使用指定分辨率的黑色背景。
- 支持选择视频帧率和图片分辨率。
- 提供每个音频的处理状态、进度和整体进度。
- CLI 支持保留中间分段、断点继续以及对已有分段单独执行合并。
- 常用输出参数保存到用户数据目录，关闭、重启或切换素材后继续使用。
- 工作区记住素材目录、最近目录、添加的图片及手动分配。
- 可以通过原生选择器指定输出目录，完成后直接打开结果所在文件夹。
- 同一套界面通过 REST API 扫描素材、提交任务、查看进度和取消任务。

## 环境要求

- Windows 10 / 11
- Python 3.11 或更高版本
- FFmpeg 和 FFprobe
- `pywebview>=6.2,<7` 和 Microsoft Edge WebView2 Runtime
- PyInstaller，用于构建 EXE

FFmpeg / FFprobe 优先读取 `AUDIOVTTFORGE_FFMPEG`、`AUDIOVTTFORGE_FFPROBE` 环境变量，其次使用 `D:\tools\ffmpeg\ffmpeg-9.0.1-essentials_build\bin\` 中的对应程序，再查找 `PATH`。可运行下方的 `doctor` 命令确认实际路径。Windows 11 和多数 Windows 10 电脑已安装 WebView2；缺失时请安装 Microsoft Edge WebView2 Evergreen Runtime。

## 运行源码

### 桌面应用

```powershell
python -m pip install -r requirements.txt
python -m audiovttforge.desktop
```

Windows 下推荐双击项目根目录的 `start_desktop.bat`。它会打开独立应用窗口，不打开浏览器，也无需保留服务控制台。启动器优先使用 `D:\tools\audiovttforge-desktop\Scripts\python.exe`，其次使用系统 Python；如需自定义，设置 `AUDIOVTTFORGE_PYTHON` 为目标 `python.exe` 的完整路径。对应环境存在 `pythonw.exe` 时使用它隐藏控制台，启动错误会显示在原生提示框中。

本文 Python 命令均从项目目录执行。安装依赖、运行 CLI 和测试时应选用与启动器相同的解释器；例如本机可将命令中的 `python` 替换为 `& 'D:\tools\audiovttforge-desktop\Scripts\python.exe'`。`AUDIOVTTFORGE_PYTHON` 只供启动器和构建脚本选择解释器，不会改变 PowerShell 中 `python` 命令的解析。

日常只需使用 `start_desktop.bat` 或构建后的 `Soundweaver.exe`。前端源码位于 `audiovttforge/web_static/`，窗口启动入口为 `audiovttforge/desktop.py`。

桌面使用流程：

1. 点击“选择音频文件夹”，在 Windows 对话框中选择音频和字幕所在目录；选定后自动扫描。
2. 确认音频和 VTT/LRC 字幕匹配结果；需要额外图片时点击“添加图片”，可以一次多选并从不同文件夹继续添加。
3. 默认输出到素材目录；需要时通过输出目录的选择按钮另选位置。
4. 调整图片分配和输出参数，开始渲染；完成后直接打开结果所在目录。再次使用同一素材时可以从最近目录选择。

关闭窗口时，如果还有正在执行的任务，程序会提示确认；确认退出后取消任务并停止本地服务及媒体进程。暂时不提供后台托盘运行。跨目录选择音频与合并已有分段可使用下方的 CLI JSON 输入与 `merge` 命令。

默认只监听 `127.0.0.1`，不会把素材发送到远程服务。
素材也可以直接输入本地目录路径并扫描。因为服务和界面运行在同一台电脑上，
服务可以直接读取这个本地目录；扫描结果会在页面内按数字自然顺序展示音频、图片、VTT/LRC 字幕以及缺失字幕警告。
确认目录内容后，前端会为每段音频显示图片选择器，默认沿用原来的自动分配规则，也可以手动指定某张图片；
同时可以选择 1 到 10 个并行进程。前端只提交路径、图片分配和渲染参数，素材本身不会通过浏览器上传。旧的上传接口仍保留，供其他 API
调用方兼容使用。
可以点击“字幕设置”打开独立设置窗口，调整字幕模式、字体、字号、文字颜色、边缘颜色与宽度、阴影颜色与深度、底部边距；这些选项会随 REST 请求传入渲染引擎。勾选“合并输出总 VTT 字幕”后，程序按音频处理顺序合并各段 VTT/LRC 字幕，并将每段时间戳平移到合并视频的全局时间轴。此选项独立于字幕模式，因此即使选择“不处理字幕”，仍可单独生成总 VTT；缺少字幕的音频段也会计入后续时间偏移。没有有效字幕条目时会生成只有 `WEBVTT` 文件头的有效空字幕文件。
设置区同时提供实时字幕效果预览，可修改示例文字确认样式；它不会替代最终视频渲染。
输出目录默认使用素材目录，也可以在提交前改为其他本机目录。服务先在任务缓存目录完成渲染，
再校验并发布最终 MP4；启用总字幕时也会发布同名 VTT sidecar（例如 `episode.mp4` 与 `episode.vtt`）。确认目标文件写入成功后会立即清理该任务的缓存文件。两个文件分别发布，不能作为一对原子替换。

### 设置与工作区

桌面版默认把用户数据保存到 `%LOCALAPPDATA%\AudioVTTForge\desktop\`，与项目源码和安装目录分开：

- `settings.json`：帧率、画面宽度、并行数、音频编码、字幕模式与合并选项、字体和字幕样式等常用参数。
- `workspace.json`：当前素材与输出目录、输出文件名、最近目录、图片的完整路径及音频到图片的分配。

修改参数后自动保存；界面会显示保存状态，失败时可以重试。关闭前会等待最后一次设置写入，保存失败时保留窗口并提示。再次启动时先恢复参数，再扫描上次素材。即使上次素材目录不存在，常用参数仍可使用。首次运行新版会从原工作区中的设置迁移到 `settings.json`，已有独立参数优先。开发验证可以通过 `python -m audiovttforge.desktop --data-root <目录>` 使用独立数据目录。

### REST API 与前端开发调试

开发时可单独启动共享服务，以测试 REST API 或调试同一份前端：

```powershell
python -m audiovttforge.web --port 8765 --data-root .\tmp\web-dev --open-browser
```

开发浏览器使用 `web_static/` 中的同一份前端，不提供单独的浏览器应用、启动器或打包产物。浏览器内没有桌面文件选择器桥接，可以手动输入路径调试扫描和渲染。服务运行期间请保留控制台，按 Ctrl+C 停止；省略 `--open-browser` 可只启动 API 服务。

上例会创建 `tmp/web-dev` 保存设置、工作区和任务缓存，并打开浏览器。省略 `--data-root` 时，独立服务默认使用 `%LOCALAPPDATA%\AudioVTTForge\web\`，也可通过 `AUDIOVTTFORGE_WEB_ROOT` 指定。数据目录隔离只影响设置与缓存；任务的最终 MP4/VTT 仍写入所选输出目录。

如需测试真实桌面 WebView，可用 `python -m audiovttforge.desktop --debug-port 18931` 启动，再通过 Playwright 的 `chromium.connectOverCDP("http://127.0.0.1:18931")` 连接。调试端口默认关闭。

核心资源如下：

```text
GET  /api/v1/health
GET  /api/v1/capabilities
GET  /api/v1/workspace
POST /api/v1/workspace
GET  /api/v1/jobs
POST /api/v1/sources/scan
GET  /api/v1/sources/image?directory={path}&name={filename}
GET  /api/v1/sources/image?path={image_absolute_path}
POST /api/v1/uploads
POST /api/v1/jobs
GET  /api/v1/jobs/{id}
GET  /api/v1/jobs/{id}/events?after={seq}
GET  /api/v1/jobs/{id}/download
GET  /api/v1/jobs/{id}/subtitle
DELETE /api/v1/jobs/{id}
```

`GET /api/v1/capabilities` 给出字幕模式、渲染默认值和可选项，网页的下拉框由它填充，不再各自声明一份默认值。
`GET /api/v1/workspace` 读取保存的常用参数和工作区，`POST` 保存修改。桌面版与独立开发服务各自使用自己的数据目录。
`GET /api/v1/jobs` 返回最近的任务列表。

`POST /api/v1/sources/scan` 接收 `{ "path": "D:\\AudioVTT\\episode01" }`，返回目录内按自然顺序整理的
音频、图片和 VTT/LRC 字幕清单。字幕优先按 `音频文件名.vtt`、`音频文件名.lrc`，再按不带音频扩展名的同名文件匹配。扫描结果里每段音频都带有默认图片分配（`automatic_image`），网页直接展示该结果，不再自行计算分配规则。随后 `POST /api/v1/jobs` 可以使用 `{ "source_dir": "..." }` 创建任务，返回
`202 Accepted`；前端通过任务资源和事件资源轮询进度。`output_dir` 可指定最终输出目录，省略时本地素材任务默认输出到素材目录；下载资源仍可用于另存副本。
提交后网页会把任务 id 记录在浏览器会话中，刷新页面会自动接回正在进行的任务并继续显示进度。
事件资源只保留最近若干条进度行，完整日志仍写入任务目录内的事件文件。
扫描请求还可以传入 `image_paths` 数组，将其他目录中的图片加入预览清单；每张图片包含完整 `path`，不同目录中的同名图片不会混淆。图片预览资源支持完整路径，只提供本次服务已经扫描登记的图片。任务提交时可用 `image_paths` 明确指定本次使用的完整图片列表，`assignments` 引用其索引；提交时缺失的图片会返回校验错误。
扫描目录时，默认输出名取完整目录路径中最长的目录名片段并添加 `.mp4`；用户可以在渲染前修改。
渲染开始后可调用 `DELETE /api/v1/jobs/{id}` 取消任务。服务会结束正在运行的 FFmpeg/FFprobe 进程，
清理本次任务目录，并保留原素材目录；网页中的路径、参数和图片分配也会保留，便于调整后重新提交。
共享前端静态文件位于 `audiovttforge/web_static/`，服务端入口位于
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

`run` 会将 JSONL 事件写入输出目录的 `.result.events.jsonl`，失败时保留中间工作目录和每个分段的 FFmpeg 日志。任务 JSON 可设置 `"merge_subtitles": true`，成功后在 MP4 同目录生成同名 `.vtt` 文件；`--keep-work` 保留成功任务的中间文件，`--resume` 跳过已经生成的分段。`--workers 1` 适合复现问题，CLI 的标准输出也会逐行输出同样的机器可读事件。

FFmpeg 和 FFprobe 默认使用源码中的 Windows 路径，也可以通过 `AUDIOVTTFORGE_FFMPEG`、`AUDIOVTTFORGE_FFPROBE` 环境变量或 CLI 参数覆盖。

## 构建 EXE

执行：

```powershell
.\build.ps1
```

该脚本默认构建桌面应用的 onedir 目录版，不附带控制台，EXE 位于：

```text
dist\Soundweaver\Soundweaver.exe
```

目录版不需要在启动时解包到 `%TEMP%`，适合日常使用。若确实需要单文件版本，可以执行 `.\build.ps1 -Mode onefile`；输出为 `dist/Soundweaver.exe`，需同时保留旁边的 `Soundweaver.exe.config`，并依赖可写的 `%TEMP%`。构建优先使用与启动器相同的 Python 环境，也可以传入 `-Python` 指定完整路径。

构建工作、spec 文件和暂存产物都位于 `tmp/build/`。成功后把程序移动到 `dist`，旧版本移入回收站；若旧程序正在运行导致替换失败，新构建保留在 `tmp/build/` 并报告路径。分发时需要复制整个 `dist/Soundweaver/` 目录；目标电脑仍需安装 WebView2 Runtime，并确保 FFmpeg / FFprobe 路径可用。

构建包含织声的 EXE 图标、窗口图标、网页图标与 Windows 产品名称。改名前的 `dist/AudioVTTForge/` 若仍存在，是旧产物；新版本请从 `dist/Soundweaver/Soundweaver.exe` 启动。

当前打包元数据 `assets/branding/windows-version-info.txt` 的版本字段为 `0.0.0.0`，尚不能用于区分每次源码构建。已有 `dist` 目录不代表它已包含最新源码修改。

`diagnose-temp.bat` 调用 [环境诊断脚本](scripts/Diagnose-Environment.ps1)，默认只读检查临时目录与两种新产物路径，明确显示“未测试写入权限”，最后等待 Enter。命令行加 `-NoPause` 可取消等待；`-TempRoot <已有目录>` 可指定诊断目录。诊断不会启动应用，也不能代替 WebView2、FFmpeg 或真实渲染验收。

只有显式加 `-WriteProbe` 才会测试文件写入和读回。探针使用指定临时目录下的唯一子目录，校验解析后绝对路径边界，拒绝链接目录及其祖先，不复用既有文件。完成后仅尝试移入回收站；回收或路径复核失败时报告保留路径并返回非零，不退回永久删除，也不处理旧 `_MEI` 或其他临时文件。以下示例把探针限定在项目 `tmp` 内：

```powershell
$probeRoot = Join-Path (Get-Location) 'tmp\environment-probes'
New-Item -ItemType Directory -Path $probeRoot -Force | Out-Null
.\diagnose-temp.bat -TempRoot $probeRoot -WriteProbe -NoPause
```

构建只生成桌面应用，通过 `--add-data` 包含唯一前端 `web_static` 和 pywebview 所需的 WebView2 桥接资源。

桌面验证应包含：程序启动、原生目录选择、字幕与图片预览、短任务渲染、打开输出目录、带任务关窗取消，以及重启后恢复设置。API 测试仍可通过 `GET /api/v1/health` 检查服务。

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

字幕文件存在但不含任何字幕条目时（例如只有 `WEBVTT` 头的占位文件），程序按"无字幕"处理并继续渲染，同时记录一条 `字幕为空` 事件；只有字幕内容写错时间戳这类真正的解析错误才会让任务失败，报错信息会带上出问题的字幕文件路径。素材目录、输出目录或输出文件名里含空格、引号、方括号、逗号等字符都可以正常渲染。

## 图片分配规则

默认情况下，图片仍按照音频顺序自动分组分配。

例如有 5 个音频和 2 张图片：

- 第 1、2、3 个音频使用第 1 张图片；
- 第 4、5 个音频使用第 2 张图片。

扫描音频和图片后，可以逐条指定图片。每个音频都可以单独指定图片，同一张图片也可以分配给多个音频；选择“自动”可以恢复该音频的默认分配规则。

“添加图片”可以一次选择多张图片，也可以重复选择其他目录。不同目录中的同名图片按完整路径区分；原文件留在原位置，程序直接读取，不复制到素材目录、不移动。手动添加的外部图片可以单张移出工作区，操作不会删除原文件；素材目录内的图片仍由目录扫描管理。桌面版保存当前工作区的图片完整路径和手动分配，重新打开后恢复仍然有效的分配。

## 输出和中间文件

输出 MP4 会根据音频所在目录和输入文件自动生成。处理过程中会创建并行工作目录，用于保存中间视频分段。

确认合成完成后，程序会清理本次生成的中间文件。如果处理中断，可以使用已有分段执行单独合并，或重新开始处理。

## 架构和测试

`audiovttforge` 包含媒体解析、任务模型、FFmpeg engine、CLI 和 REST API。`desktop.py` 管理 pywebview 窗口、本地文件桥接、设置和服务生命周期；唯一的前端位于 `web_static/`，通过 REST API 管理任务。`web.py` 是桌面应用共享的 REST 服务，也用于开发测试。

运行 headless 测试前，所选 Python 环境需已安装 `pytest`（运行依赖文件 `requirements.txt` 不包含它）。从项目目录运行，将测试文件留在新的 `tmp` 子目录，关闭字节码与 pytest 缓存写入：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$testRoot = Join-Path (Get-Location) ('tmp\pytest-' + [guid]::NewGuid().ToString('N'))
python -B -m pytest -q -p no:cacheprovider --basetemp $testRoot
```

每次使用新的 `$testRoot`；pytest 会清理复用的 `--basetemp` 目录。测试会写入临时任务与模拟媒体、启动测试用子进程和本机 HTTP 服务，不打开真实桌面窗口；这些检查通过也不等于真实 WebView2 与媒体渲染已验收。只检查命令入口和媒体工具时可用 `python -B -m audiovttforge --help`、`python -B -m audiovttforge doctor`，不会提交渲染任务。

## 许可证

如果计划公开发布或再分发本项目，请在发布前添加合适的许可证文件。

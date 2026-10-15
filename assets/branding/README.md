# 织声 · Soundweaver

织声是夜莺播放器的媒体工坊。对外使用中文名「织声」及英文名 `Soundweaver`；源码目录、Python 模块 `audiovttforge`、环境变量 `AUDIOVTTFORGE_*`、API 服务标识和用户数据目录保留原值，兼容已有脚本与设置。

- `soundweaver-master.png`：1024×1024 正方形、不透明原始母版。
- `soundweaver.prompt.txt`：2026-10-13 使用 `gpt-image-2`、high 质量生成的完整提示词。
- `../../audiovttforge/web_static/branding/soundweaver-256.png`：256×256、透明圆角的页面标识。
- `../../audiovttforge/web_static/branding/soundweaver.ico`：Windows 窗口、任务栏、EXE 和网页图标，含 16/24/32/48/64/128/256 像素。
- `windows-version-info.txt`：打包时写入 EXE 的中文产品名称和英文文件名。

图形为深蓝月色、青绿宽纸带交织，表现音频、字幕与画面的组合；与夜莺共用月牙和冷色系。派生 PNG 和 ICO 从母版等比缩放，圆角半径为边长的 20%，保留透明圆角。

构建脚本把 `web_static` 完整打包，运行时通过 `static_root()` 定位窗口图标，兼容源码运行、onedir 和 onefile。产物入口为 `dist/Soundweaver/Soundweaver.exe`。

# AudioVTTForge

Local Windows GUI for combining WAV/MP3 audio, matching WebVTT subtitles, and still images into MP4.

## Features

- Batch WAV/MP3 import.
- Matching subtitles named `audio.wav.vtt` or `audio.mp3.vtt`.
- Image drag-and-drop when `tkinterdnd2` is installed.
- Per-audio parallel rendering with AAC output by default.
- Full-image presentation with proportional scaling and letterboxing.
- Per-file status table plus overall progress.
- Resume-friendly intermediate segment directory.
- Separate merge action for existing segments.
- Persistent user settings in `%APPDATA%\AudioVttToMp4Parallel\settings.json`.

## Requirements

- Windows
- Python 3.11+
- FFmpeg and FFprobe at the configured local path
- Optional: `tkinterdnd2` for native file drag-and-drop
- PyInstaller for EXE builds

## Run

```powershell
python .\audio_vtt_to_mp4_gui.py
```

## Build

```powershell
.\build.ps1
```

The EXE is written to `dist\AudioVTTForge.exe`.

## Git Workflow

This directory is an independent Git repository. Build outputs, Python caches, local logs, and generated EXE files are ignored.

Create a commit for current changes:

```powershell
.\auto_commit.ps1 -Message "describe the change"
```

Watch the project and automatically commit detected changes:

```powershell
.\watch_and_commit.ps1
```

To also push each commit to the configured remote:

```powershell
.\watch_and_commit.ps1 -Push
```

Do not put GitHub passwords or access tokens in project files. Configure GitHub authentication separately on the machine.

## Input Naming

```text
01.wav
01.wav.vtt
cover.jpg
```

Images are assigned to consecutive audio groups in order. For example, five audio files and two images use the first image for the first two audio files and the second image for the remaining three.

## License

Add a license before publishing if this project is intended for redistribution.

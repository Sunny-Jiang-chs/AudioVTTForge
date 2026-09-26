#!/usr/bin/env python3
"""AudioVTTForge: parallel audio + VTT + image to MP4 GUI."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES
except ImportError:
    DND_FILES = None

VIDEO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
AUDIO_EXTENSIONS = {".wav", ".mp3"}
FFMPEG = Path(r"D:\tools\ffmpeg\ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\tools\ffmpeg\ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe")
CONFIG = Path(os.environ.get("APPDATA", str(Path.home()))) / "AudioVttToMp4Parallel" / "settings.json"


@dataclass
class Cue:
    start: int
    end: int
    text: str


def env_for(tool: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(tool.parent) + os.pathsep + env.get("PATH", "")
    return env


def parse_time(value: str) -> int:
    match = re.match(r"^(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid timestamp: {value}")
    return ((int(match.group(1) or 0) * 60 + int(match.group(2))) * 60 + int(match.group(3))) * 1000 + int(match.group(4))


def srt_time(value: int) -> str:
    hours, rest = divmod(max(0, value), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def read_vtt(path: Path) -> list[Cue]:
    lines = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[Cue] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.upper() in {"WEBVTT", "NOTE", "STYLE", "REGION"}:
            index += 1
            continue
        if "-->" not in line:
            index += 1
            continue
        left, right = line.split("-->", 1)
        start, end = parse_time(left), parse_time(right.split()[0])
        index += 1
        text_lines = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index])
            index += 1
        text = re.sub(r"(?i)<br\s*/?>", "\n", "\n".join(text_lines))
        text = re.sub(r"(?i)</?(?:c|v|lang|i|b|u|ruby|rt)(?:\.[^ >]+)?(?:\s+[^>]*)?>", "", text)
        text = re.sub(r"<[^>]+>", "", text).strip()
        if text and end > start:
            cues.append(Cue(start, end, text))
    return cues


def write_srt(cues: list[Cue], path: Path) -> None:
    lines: list[str] = []
    for number, cue in enumerate(cues, 1):
        lines += [str(number), f"{srt_time(cue.start)} --> {srt_time(cue.end)}", cue.text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


AUTO_ASSIGNMENT_PREFIX = "自动: "


def automatic_image_index(audio_index: int, audio_count: int, image_count: int) -> int:
    if audio_count < 1 or image_count < 1:
        raise ValueError("audio_count and image_count must be positive")
    if not 0 <= audio_index < audio_count:
        raise ValueError("audio_index is outside the audio range")
    return min(image_count - 1, audio_index * image_count // audio_count)


def video_dimensions(width: int) -> tuple[int, int]:
    if width < 2 or width % 2:
        raise ValueError("video width must be a positive even number")
    height = round(width * 9 / 16)
    return width, height + height % 2


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AudioVTTForge")
        self.root.geometry("1120x760")
        self.root.minsize(980, 640)
        self.audio: list[Path] = []
        self.images: list[Path] = []
        self.manual_assignments: dict[str, str] = {}
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.running = False
        self.output = tk.StringVar()
        self.subtitle = tk.StringVar(value="burnin")
        self.fps = tk.StringVar(value="2")
        self.width = tk.StringVar(value="1920")
        self.workers = tk.StringVar(value="2")
        self.total_progress = tk.DoubleVar()
        self.status = tk.StringVar(value="导入音频和图片后开始。")
        self.ffmpeg = FFMPEG
        self.ffprobe = FFPROBE
        self._load()
        self._build()
        self._refresh()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)

    def _build(self) -> None:
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Muted.TLabel", foreground="#667085")
        style.configure("Status.Treeview", rowheight=28)
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ttk.Label(header, text="AudioVTTForge", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="并行生成音频字幕视频，按文件状态清晰追踪", style="Muted.TLabel").pack(anchor="w", pady=(3, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for label, command in (
            ("批量导入音频", self.add_audio),
            ("导入图片", self.add_images),
            ("合并已有分段", self.merge_existing),
            ("清空", self.clear),
        ):
            ttk.Button(actions, text=label, command=command).pack(side="left", padx=(0, 7))
        ttk.Label(actions, text="AAC 优先 · 图片完整显示 · 分段可恢复", style="Muted.TLabel").pack(side="left", padx=12)

        content = ttk.Panedwindow(outer, orient="vertical")
        content.grid(row=2, column=0, sticky="nsew")
        lists = ttk.Panedwindow(content, orient="horizontal")
        content.add(lists, weight=2)
        audio_frame = ttk.Labelframe(lists, text="音频顺序", padding=8)
        image_frame = ttk.Labelframe(lists, text="图片顺序（按音频段分配）", padding=8)
        lists.add(audio_frame, weight=1)
        lists.add(image_frame, weight=1)
        audio_frame.rowconfigure(0, weight=1)
        audio_frame.columnconfigure(0, weight=1)
        image_frame.rowconfigure(0, weight=1)
        image_frame.columnconfigure(0, weight=1)
        self.audio_list = tk.Listbox(audio_frame, activestyle="none", borderwidth=0, highlightthickness=0)
        self.image_list = tk.Listbox(image_frame, activestyle="none", borderwidth=0, highlightthickness=0)
        self.audio_list.grid(row=0, column=0, sticky="nsew")
        self.image_list.grid(row=0, column=0, sticky="nsew")
        if DND_FILES:
            self.image_list.drop_target_register(DND_FILES)
            self.image_list.dnd_bind("<<Drop>>", lambda event: self.drop(event.data))

        assignment_frame = ttk.Labelframe(content, text="音频-图片分配", padding=8)
        content.add(assignment_frame, weight=2)
        assignment_frame.rowconfigure(0, weight=1)
        assignment_frame.columnconfigure(0, weight=1)
        self.assignment_canvas = tk.Canvas(assignment_frame, borderwidth=0, highlightthickness=0)
        assignment_scrollbar = ttk.Scrollbar(assignment_frame, orient="vertical", command=self.assignment_canvas.yview)
        self.assignment_canvas.configure(yscrollcommand=assignment_scrollbar.set)
        self.assignment_canvas.grid(row=0, column=0, sticky="nsew")
        assignment_scrollbar.grid(row=0, column=1, sticky="ns")
        self.assignment_body = ttk.Frame(self.assignment_canvas)
        self.assignment_body.columnconfigure(0, weight=1)
        self.assignment_body.columnconfigure(1, weight=1)
        self.assignment_window = self.assignment_canvas.create_window((0, 0), window=self.assignment_body, anchor="nw")
        self.assignment_body.bind(
            "<Configure>",
            lambda _event: self.assignment_canvas.configure(scrollregion=self.assignment_canvas.bbox("all")),
        )
        self.assignment_canvas.bind(
            "<Configure>",
            lambda event: self.assignment_canvas.itemconfigure(self.assignment_window, width=event.width),
        )

        progress_frame = ttk.Labelframe(content, text="任务状态", padding=8)
        content.add(progress_frame, weight=3)
        progress_frame.rowconfigure(1, weight=1)
        progress_frame.columnconfigure(0, weight=1)
        ttk.Progressbar(progress_frame, variable=self.total_progress, maximum=100).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.task_table = ttk.Treeview(progress_frame, columns=("state", "progress", "detail"), show="headings", style="Status.Treeview")
        for column, title, width in (("state", "状态", 100), ("progress", "进度", 100), ("detail", "当前信息", 560)):
            self.task_table.heading(column, text=title)
            self.task_table.column(column, width=width, anchor="w")
        self.task_table.grid(row=1, column=0, sticky="nsew")

        settings = ttk.Labelframe(outer, text="输出设置", padding=10)
        settings.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        settings.columnconfigure(1, weight=1)
        self._row(settings, 0, "输出 MP4", ttk.Entry(settings, textvariable=self.output), ttk.Button(settings, text="选择", command=self.choose_output))
        self._row(settings, 1, "字幕", ttk.Combobox(settings, textvariable=self.subtitle, values=("burnin", "embedded", "none"), state="readonly"))
        self._row(settings, 2, "帧率", ttk.Combobox(settings, textvariable=self.fps, values=("1", "2", "5", "10", "24"), state="readonly"))
        self._row(settings, 3, "图片宽度", ttk.Combobox(settings, textvariable=self.width, values=("1080", "1280", "1920", "2560"), state="readonly"))
        self._row(settings, 4, "并行进程数", ttk.Entry(settings, textvariable=self.workers))
        self.start_button = ttk.Button(settings, text="开始合成", command=self.start)
        self.start_button.grid(row=5, column=2, sticky="e", pady=(6, 0))
        ttk.Label(settings, text="图片保持完整，按比例缩放到 16:9 画布；并发数取输入值与音频数量的较小值", style="Muted.TLabel").grid(row=5, column=1, sticky="w", pady=(6, 0))
        ttk.Label(outer, textvariable=self.status, anchor="w", style="Muted.TLabel").grid(row=4, column=0, sticky="ew", pady=(8, 0))

    def _row(self, parent: ttk.Frame, row: int, label: str, widget: tk.Widget, extra: tk.Widget | None = None) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
        widget.grid(row=row, column=1, sticky="ew", pady=3)
        if extra:
            extra.grid(row=row, column=2, padx=(8, 0))

    def _refresh(self) -> None:
        self.audio_list.delete(0, tk.END)
        for path in self.audio:
            self.audio_list.insert(tk.END, f"{path.name}  {'VTT OK' if path.with_name(path.name + '.vtt').is_file() else '缺 VTT'}")
        self.image_list.delete(0, tk.END)
        for path in self.images:
            self.image_list.insert(tk.END, path.name)
        self._refresh_assignment_rows()

    def _image_label(self, index: int) -> str:
        return f"{index + 1}. {self.images[index].name}"

    def _automatic_choice(self, audio_index: int) -> str:
        image_index = automatic_image_index(audio_index, len(self.audio), len(self.images))
        return f"{AUTO_ASSIGNMENT_PREFIX}{self._image_label(image_index)}"

    def _assignment_choices(self, audio_index: int) -> list[str]:
        return [self._automatic_choice(audio_index)] + [self._image_label(index) for index in range(len(self.images))]

    def _choice_for_audio(self, audio_index: int) -> str:
        audio_key = str(self.audio[audio_index])
        manual_image = self.manual_assignments.get(audio_key)
        if manual_image:
            for image_index, image in enumerate(self.images):
                if str(image) == manual_image:
                    return self._image_label(image_index)
        return self._automatic_choice(audio_index)

    def _refresh_assignment_rows(self) -> None:
        valid_audio = {str(path) for path in self.audio}
        valid_images = {str(path) for path in self.images}
        self.manual_assignments = {
            audio: image
            for audio, image in self.manual_assignments.items()
            if audio in valid_audio and image in valid_images
        }
        for child in self.assignment_body.winfo_children():
            child.destroy()
        ttk.Label(self.assignment_body, text="音频").grid(row=0, column=0, sticky="w", padx=(2, 8), pady=(0, 5))
        ttk.Label(self.assignment_body, text="使用图片").grid(row=0, column=1, sticky="ew", padx=(8, 2), pady=(0, 5))
        if not self.images:
            ttk.Label(self.assignment_body, text="请先导入图片").grid(
                row=1, column=0, columnspan=2, sticky="w", padx=2, pady=4
            )
            return
        for audio_index, audio in enumerate(self.audio, 1):
            ttk.Label(self.assignment_body, text=audio.name).grid(
                row=audio_index, column=0, sticky="w", padx=(2, 8), pady=2
            )
            variable = tk.StringVar(value=self._choice_for_audio(audio_index - 1))
            combo = ttk.Combobox(
                self.assignment_body,
                textvariable=variable,
                values=self._assignment_choices(audio_index - 1),
                state="readonly",
            )
            combo.grid(row=audio_index, column=1, sticky="ew", padx=(8, 2), pady=2)
            combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, path=audio, value=variable: self._assignment_changed(path, value),
            )

    def _assignment_changed(self, audio: Path, variable: tk.StringVar) -> None:
        selection = variable.get()
        audio_key = str(audio)
        if selection.startswith(AUTO_ASSIGNMENT_PREFIX):
            self.manual_assignments.pop(audio_key, None)
        else:
            for image_index, image in enumerate(self.images):
                if selection == self._image_label(image_index):
                    self.manual_assignments[audio_key] = str(image)
                    break
        self.save()

    def resolve_assignments(self) -> list[int]:
        image_indices = {str(image): index for index, image in enumerate(self.images)}
        assignments: list[int] = []
        for audio_index, audio in enumerate(self.audio):
            manual_image = self.manual_assignments.get(str(audio))
            if manual_image in image_indices:
                assignments.append(image_indices[manual_image])
            else:
                assignments.append(automatic_image_index(audio_index, len(self.audio), len(self.images)))
        return assignments

    def add_audio(self) -> None:
        for item in filedialog.askopenfilenames(filetypes=[("音频", "*.wav *.mp3")]):
            path = Path(item)
            if path.suffix.lower() in AUDIO_EXTENSIONS and path not in self.audio:
                self.audio.append(path)
        self._refresh()
        if self.audio and not self.output.get():
            self.output.set(str(self.audio[0].with_name(self.audio[0].stem + "_merged.mp4")))
        self.save()

    def add_images(self) -> None:
        for item in filedialog.askopenfilenames(filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp")]):
            path = Path(item)
            if path.suffix.lower() in VIDEO_EXTENSIONS and path not in self.images:
                self.images.append(path)
        self._refresh()
        self.save()

    def drop(self, data: str) -> None:
        for item in self.root.tk.splitlist(data):
            path = Path(item)
            if path.suffix.lower() in VIDEO_EXTENSIONS and path not in self.images:
                self.images.append(path)
        self._refresh()
        self.save()

    def choose_output(self) -> None:
        item = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
        if item:
            self.output.set(item)
            self.save()

    def clear(self) -> None:
        if not self.running:
            self.audio.clear()
            self.images.clear()
            self.manual_assignments.clear()
            self.output.set("")
            self._refresh()
            self.save()

    def _load(self) -> None:
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.audio = [Path(p) for p in data.get("audio_paths", []) if Path(p).is_file()]
        self.images = [Path(p) for p in data.get("image_paths", []) if Path(p).is_file()]
        raw_assignments = data.get("image_assignments", {})
        if isinstance(raw_assignments, dict):
            self.manual_assignments = {str(audio): str(image) for audio, image in raw_assignments.items()}
        self.output.set(data.get("output", ""))
        for variable, key in ((self.subtitle, "subtitle"), (self.fps, "fps"), (self.width, "width"), (self.workers, "workers")):
            if key in data:
                variable.set(str(data[key]))
        if self.audio and not self.output.get():
            self.output.set(str(self.audio[0].with_name(self.audio[0].stem + "_merged.mp4")))

    def save(self) -> None:
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps({
                "audio_paths": [str(p) for p in self.audio],
                "image_paths": [str(p) for p in self.images],
                "image_assignments": self.manual_assignments,
                "output": self.output.get(),
                "subtitle": self.subtitle.get(),
                "fps": self.fps.get(),
                "width": self.width.get(),
                "workers": self.workers.get(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def close(self) -> None:
        self.save()
        self.root.destroy()

    def set_tasks(self) -> None:
        for item in self.task_table.get_children():
            self.task_table.delete(item)
        for index, path in enumerate(self.audio):
            self.task_table.insert("", "end", iid=str(index), values=("等待", "0%", path.name))

    def task(self, index: int, state: str, progress: str, detail: str) -> None:
        self.events.put(("task", (index, state, progress, detail)))

    def start(self) -> None:
        if self.running:
            return
        if not self.audio or not self.images:
            messagebox.showwarning("输入不完整", "请导入音频和至少一张图片。")
            return
        missing = [path.name for path in self.audio if self.subtitle.get() != "none" and not path.with_name(path.name + ".vtt").is_file()]
        if missing:
            messagebox.showerror("缺少 VTT", "\n".join(missing))
            return
        if not self.ffmpeg.is_file() or not self.ffprobe.is_file():
            messagebox.showerror("FFmpeg 不可用", "固定 FFmpeg/FFprobe 路径不存在。")
            return
        try:
            requested = int(self.workers.get())
            if requested < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("进程数无效", "并行进程数必须是正整数。")
            return
        assignment = self.resolve_assignments()
        self.save()
        self.running = True
        self.start_button.configure(state="disabled")
        self.total_progress.set(1)
        self.status.set("已开始：读取音频时长...")
        self.set_tasks()
        threading.Thread(
            target=self.run,
            args=(min(requested, len(self.audio)), assignment),
            daemon=True,
        ).start()

    def probe(self, audio: Path) -> float:
        args = [str(self.ffprobe), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration", "-of", "json", str(audio)]
        for attempt in range(3):
            try:
                result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, env=env_for(self.ffprobe), cwd=str(self.ffprobe.parent))
                return float(json.loads(result.stdout)["streams"][0]["duration"])
            except subprocess.CalledProcessError as exc:
                if (exc.returncode & 0xFFFFFFFF) != 0xC0000142 or attempt == 2:
                    raise RuntimeError(f"FFprobe 失败：{audio.name}\n{exc.stderr or exc.stdout}") from exc
                time.sleep(0.8 * (attempt + 1))

    def merge_segments(
        self,
        segments: list[Path],
        work: Path,
        output: Path,
        concat_name: str,
        merge_name: str,
        log_name: str,
    ) -> None:
        concat = work / concat_name
        concat.write_text("\n".join(f"file '{path.resolve()}'" for path in segments), encoding="utf-8")
        merge_output = work / merge_name
        join_log = work / log_name
        output_args = [
            str(self.ffmpeg),
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(merge_output),
        ]
        try:
            with join_log.open("w", encoding="utf-8") as log:
                result = subprocess.run(
                    output_args,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env_for(self.ffmpeg),
                    check=False,
                )
        except OSError as exc:
            try:
                join_log.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            except OSError:
                pass
            raise RuntimeError(f"合并进程启动失败，日志：{join_log}") from exc
        if result.returncode != 0:
            try:
                details = join_log.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                details = ""
            raise RuntimeError(f"合并失败，日志：{join_log}\n{details[-2000:]}")
        try:
            os.replace(merge_output, output)
        except OSError as exc:
            raise RuntimeError(f"无法替换输出文件，可能正在被其他程序占用：{output}") from exc

    def run(self, workers: int, assignment: list[int]) -> None:
        work: Path | None = None
        try:
            durations = [self.probe(path) for path in self.audio]
            width = int(self.width.get())
            width, height = video_dimensions(width)
            fps = int(self.fps.get())
            output = Path(self.output.get())
            output.parent.mkdir(parents=True, exist_ok=True)
            work = output.parent / f".{output.stem}_parallel_work"
            work.mkdir(parents=True, exist_ok=True)

            def render(index: int) -> Path:
                audio = self.audio[index]
                segment = work / f"{index:04d}.mp4"
                duration = durations[index]
                image = self.images[assignment[index]]
                vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps},setpts=N/FRAME_RATE/TB,setsar=1"
                args = [str(self.ffmpeg), "-hide_banner", "-y", "-loop", "1", "-framerate", str(fps), "-t", f"{duration:.3f}", "-i", str(image), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-vf", vf, "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "512k", "-ar", "48000", "-ac", "2", "-t", f"{duration:.3f}"]
                if self.subtitle.get() == "burnin":
                    srt = work / f"{index:04d}.srt"
                    write_srt(read_vtt(audio.with_name(audio.name + ".vtt")), srt)
                    subtitle_path = str(srt).replace("\\", "/").replace(":", "\\:")
                    args[args.index("-vf") + 1] += f",subtitles='{subtitle_path}':force_style='FontName=Microsoft YaHei,FontSize=42,Outline=2,Shadow=1,Alignment=2,MarginV=48'"
                elif self.subtitle.get() == "embedded":
                    args += ["-i", str(audio.with_name(audio.name + ".vtt")), "-map", "2:0", "-c:s", "mov_text"]
                args += ["-movflags", "+faststart", str(segment)]
                log = work / f"{index:04d}.log"
                with log.open("w", encoding="utf-8") as stream:
                    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env_for(self.ffmpeg))
                    assert process.stdout is not None
                    for line in process.stdout:
                        stream.write(line)
                        if "time=" in line:
                            self.task(index, "处理中", line.strip()[-120:], audio.name)
                    if process.wait() != 0:
                        raise RuntimeError(f"{audio.name} 失败，日志：{log}")
                return segment

            segments = [Path()] * len(self.audio)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(render, index): index for index in range(len(self.audio))}
                for future in as_completed(futures):
                    index = futures[future]
                    segments[index] = future.result()
                    self.task(index, "完成", "100%", self.audio[index].name)
                    self.events.put(("overall", 10 + sum(path.exists() for path in segments) / len(segments) * 80))
            self.merge_segments(segments, work, output, "segments.txt", "merged.mp4", "join.log")
            shutil.rmtree(work)
            self.events.put(("done", str(output)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def merge_existing(self) -> None:
        work = filedialog.askdirectory(title="选择 _parallel_work 中间目录")
        if not work:
            return
        segments = sorted(path for path in Path(work).glob("*.mp4") if path.stem.isdigit())
        if not segments:
            messagebox.showerror("没有分段", "所选目录中没有 MP4 分段。")
            return
        output = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
        if not output:
            return
        try:
            self.merge_segments(
                segments,
                Path(work),
                Path(output),
                "segments_recovery.txt",
                "merged_recovery.mp4",
                "join_recovery.log",
            )
            messagebox.showinfo("合并完成", str(output))
        except Exception as exc:
            messagebox.showerror("合并失败", str(exc))

    def poll(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "task":
                    index, state, progress, detail = value
                    self.task_table.item(str(index), values=(state, progress, detail))
                elif kind == "overall":
                    self.total_progress.set(float(value))
                elif kind == "done":
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.total_progress.set(100)
                    self.status.set(f"完成：{value}")
                    messagebox.showinfo("完成", str(value))
                elif kind == "error":
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.status.set("失败")
                    messagebox.showerror("失败", str(value))
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

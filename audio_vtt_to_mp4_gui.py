#!/usr/bin/env python3
"""Thin Tkinter shell for the headless AudioVTTForge engine."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tkinterdnd2 import DND_FILES
except ImportError:
    DND_FILES = None

from audiovttforge.engine import RenderEngine, validate_job
from audiovttforge.job import JobSpec, default_ffmpeg, default_ffprobe
from audiovttforge.media import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, automatic_image_index

CONFIG = (
    Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    / "AudioVttToMp4Parallel"
    / "settings.json"
)
AUTO_ASSIGNMENT_PREFIX = "自动: "


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AudioVTTForge")
        self.root.geometry("1120x760")
        self.root.minsize(980, 640)
        self.audio: list[Path] = []
        self.images: list[Path] = []
        self.manual_assignments: dict[str, str] = {}
        self.events: queue.Queue[dict[str, object]] = queue.Queue()
        self.running = False
        self.output = tk.StringVar()
        self.subtitle = tk.StringVar(value="burnin")
        self.fps = tk.StringVar(value="2")
        self.width = tk.StringVar(value="1920")
        self.workers = tk.StringVar(value="2")
        self.total_progress = tk.DoubleVar()
        self.status = tk.StringVar(value="导入音频和图片后开始。")
        self.ffmpeg = default_ffmpeg()
        self.ffprobe = default_ffprobe()
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
        ttk.Label(
            header,
            text="并行生成音频字幕视频，按文件状态清晰追踪",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for label, command in (
            ("批量导入音频", self.add_audio),
            ("导入图片", self.add_images),
            ("合并已有分段", self.merge_existing),
            ("清空", self.clear),
        ):
            ttk.Button(actions, text=label, command=command).pack(side="left", padx=(0, 7))
        ttk.Label(
            actions,
            text="AAC 优先 · 图片完整显示 · 分段可恢复",
            style="Muted.TLabel",
        ).pack(side="left", padx=12)

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
        self.audio_list = tk.Listbox(
            audio_frame,
            activestyle="none",
            borderwidth=0,
            highlightthickness=0,
        )
        self.image_list = tk.Listbox(
            image_frame,
            activestyle="none",
            borderwidth=0,
            highlightthickness=0,
        )
        self.audio_list.grid(row=0, column=0, sticky="nsew")
        self.image_list.grid(row=0, column=0, sticky="nsew")
        if DND_FILES:
            self.image_list.drop_target_register(DND_FILES)
            self.image_list.dnd_bind("<<Drop>>", lambda event: self.drop(event.data))

        assignment_frame = ttk.Labelframe(content, text="音频-图片分配", padding=8)
        content.add(assignment_frame, weight=2)
        assignment_frame.rowconfigure(0, weight=1)
        assignment_frame.columnconfigure(0, weight=1)
        self.assignment_canvas = tk.Canvas(
            assignment_frame,
            borderwidth=0,
            highlightthickness=0,
        )
        assignment_scrollbar = ttk.Scrollbar(
            assignment_frame,
            orient="vertical",
            command=self.assignment_canvas.yview,
        )
        self.assignment_canvas.configure(yscrollcommand=assignment_scrollbar.set)
        self.assignment_canvas.grid(row=0, column=0, sticky="nsew")
        assignment_scrollbar.grid(row=0, column=1, sticky="ns")
        self.assignment_body = ttk.Frame(self.assignment_canvas)
        self.assignment_body.columnconfigure(0, weight=1)
        self.assignment_body.columnconfigure(1, weight=1)
        self.assignment_window = self.assignment_canvas.create_window(
            (0, 0),
            window=self.assignment_body,
            anchor="nw",
        )
        self.assignment_body.bind(
            "<Configure>",
            lambda _event: self.assignment_canvas.configure(
                scrollregion=self.assignment_canvas.bbox("all")
            ),
        )
        self.assignment_canvas.bind(
            "<Configure>",
            lambda event: self.assignment_canvas.itemconfigure(
                self.assignment_window,
                width=event.width,
            ),
        )

        progress_frame = ttk.Labelframe(content, text="任务状态", padding=8)
        content.add(progress_frame, weight=3)
        progress_frame.rowconfigure(1, weight=1)
        progress_frame.columnconfigure(0, weight=1)
        ttk.Progressbar(
            progress_frame,
            variable=self.total_progress,
            maximum=100,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.task_table = ttk.Treeview(
            progress_frame,
            columns=("state", "progress", "detail"),
            show="headings",
            style="Status.Treeview",
        )
        for column, title, width in (
            ("state", "状态", 100),
            ("progress", "进度", 100),
            ("detail", "当前信息", 560),
        ):
            self.task_table.heading(column, text=title)
            self.task_table.column(column, width=width, anchor="w")
        self.task_table.grid(row=1, column=0, sticky="nsew")

        settings = ttk.Labelframe(outer, text="输出设置", padding=10)
        settings.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        settings.columnconfigure(1, weight=1)
        self._row(
            settings,
            0,
            "输出 MP4",
            ttk.Entry(settings, textvariable=self.output),
            ttk.Button(settings, text="选择", command=self.choose_output),
        )
        self._row(
            settings,
            1,
            "字幕",
            ttk.Combobox(
                settings,
                textvariable=self.subtitle,
                values=("burnin", "embedded", "none"),
                state="readonly",
            ),
        )
        self._row(
            settings,
            2,
            "帧率",
            ttk.Combobox(
                settings,
                textvariable=self.fps,
                values=("1", "2", "5", "10", "24"),
                state="readonly",
            ),
        )
        self._row(
            settings,
            3,
            "图片宽度",
            ttk.Combobox(
                settings,
                textvariable=self.width,
                values=("1080", "1280", "1920", "2560"),
                state="readonly",
            ),
        )
        self._row(
            settings,
            4,
            "并行进程数",
            ttk.Entry(settings, textvariable=self.workers),
        )
        self.start_button = ttk.Button(settings, text="开始合成", command=self.start)
        self.start_button.grid(row=5, column=2, sticky="e", pady=(6, 0))
        ttk.Label(
            settings,
            text="图片保持完整，按比例缩放到 16:9 画布；并发数取输入值与音频数量的较小值",
            style="Muted.TLabel",
        ).grid(row=5, column=1, sticky="w", pady=(6, 0))
        ttk.Label(
            outer,
            textvariable=self.status,
            anchor="w",
            style="Muted.TLabel",
        ).grid(row=4, column=0, sticky="ew", pady=(8, 0))

    def _row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        widget: tk.Widget,
        extra: tk.Widget | None = None,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=3,
        )
        widget.grid(row=row, column=1, sticky="ew", pady=3)
        if extra:
            extra.grid(row=row, column=2, padx=(8, 0))

    def _refresh(self) -> None:
        self.audio_list.delete(0, tk.END)
        for path in self.audio:
            has_vtt = path.with_name(path.name + ".vtt").is_file()
            self.audio_list.insert(
                tk.END,
                f"{path.name}  {'VTT OK' if has_vtt else '缺 VTT'}",
            )
        self.image_list.delete(0, tk.END)
        for path in self.images:
            self.image_list.insert(tk.END, path.name)
        self._refresh_assignment_rows()

    def _image_label(self, index: int) -> str:
        return f"{index + 1}. {self.images[index].name}"

    def _automatic_choice(self, audio_index: int) -> str:
        image_index = automatic_image_index(
            audio_index,
            len(self.audio),
            len(self.images),
        )
        return f"{AUTO_ASSIGNMENT_PREFIX}{self._image_label(image_index)}"

    def _assignment_choices(self, audio_index: int) -> list[str]:
        return [self._automatic_choice(audio_index)] + [
            self._image_label(index) for index in range(len(self.images))
        ]

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
        ttk.Label(self.assignment_body, text="音频").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(2, 8),
            pady=(0, 5),
        )
        ttk.Label(self.assignment_body, text="使用图片").grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(8, 2),
            pady=(0, 5),
        )
        if not self.images:
            ttk.Label(self.assignment_body, text="请先导入图片").grid(
                row=1,
                column=0,
                columnspan=2,
                sticky="w",
                padx=2,
                pady=4,
            )
            return
        for audio_index, audio in enumerate(self.audio, 1):
            ttk.Label(self.assignment_body, text=audio.name).grid(
                row=audio_index,
                column=0,
                sticky="w",
                padx=(2, 8),
                pady=2,
            )
            variable = tk.StringVar(value=self._choice_for_audio(audio_index - 1))
            combo = ttk.Combobox(
                self.assignment_body,
                textvariable=variable,
                values=self._assignment_choices(audio_index - 1),
                state="readonly",
            )
            combo.grid(
                row=audio_index,
                column=1,
                sticky="ew",
                padx=(8, 2),
                pady=2,
            )
            combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, path=audio, value=variable: self._assignment_changed(
                    path,
                    value,
                ),
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
                assignments.append(
                    automatic_image_index(
                        audio_index,
                        len(self.audio),
                        len(self.images),
                    )
                )
        return assignments

    def add_audio(self) -> None:
        for item in filedialog.askopenfilenames(filetypes=[("音频", "*.wav *.mp3")]):
            path = Path(item)
            if path.suffix.lower() in AUDIO_EXTENSIONS and path not in self.audio:
                self.audio.append(path)
        self._refresh()
        if self.audio and not self.output.get():
            self.output.set(
                str(self.audio[0].with_name(self.audio[0].stem + "_merged.mp4"))
            )
        self.save()

    def add_images(self) -> None:
        for item in filedialog.askopenfilenames(
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.bmp")]
        ):
            path = Path(item)
            if path.suffix.lower() in IMAGE_EXTENSIONS and path not in self.images:
                self.images.append(path)
        self._refresh()
        self.save()

    def drop(self, data: str) -> None:
        for item in self.root.tk.splitlist(data):
            path = Path(item)
            if path.suffix.lower() in IMAGE_EXTENSIONS and path not in self.images:
                self.images.append(path)
        self._refresh()
        self.save()

    def choose_output(self) -> None:
        item = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4")],
        )
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
        self.audio = [
            Path(path)
            for path in data.get("audio_paths", [])
            if Path(path).is_file()
        ]
        self.images = [
            Path(path)
            for path in data.get("image_paths", [])
            if Path(path).is_file()
        ]
        raw_assignments = data.get("image_assignments", {})
        if isinstance(raw_assignments, dict):
            self.manual_assignments = {
                str(audio): str(image)
                for audio, image in raw_assignments.items()
            }
        self.output.set(data.get("output", ""))
        for variable, key in (
            (self.subtitle, "subtitle"),
            (self.fps, "fps"),
            (self.width, "width"),
            (self.workers, "workers"),
        ):
            if key in data:
                variable.set(str(data[key]))
        if self.audio and not self.output.get():
            self.output.set(
                str(self.audio[0].with_name(self.audio[0].stem + "_merged.mp4"))
            )

    def save(self) -> None:
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(
                json.dumps(
                    {
                        "audio_paths": [str(path) for path in self.audio],
                        "image_paths": [str(path) for path in self.images],
                        "image_assignments": self.manual_assignments,
                        "output": self.output.get(),
                        "subtitle": self.subtitle.get(),
                        "fps": self.fps.get(),
                        "width": self.width.get(),
                        "workers": self.workers.get(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def close(self) -> None:
        self.save()
        self.root.destroy()

    def set_tasks(self) -> None:
        for item in self.task_table.get_children():
            self.task_table.delete(item)
        for index, path in enumerate(self.audio):
            self.task_table.insert(
                "",
                "end",
                iid=str(index),
                values=("等待", "0%", path.name),
            )

    def _snapshot_job(self) -> JobSpec:
        if not self.output.get().strip():
            raise ValueError("请先选择输出 MP4 路径。")
        try:
            fps = int(self.fps.get())
            width = int(self.width.get())
            workers = int(self.workers.get())
        except ValueError as exc:
            raise ValueError("帧率、图片宽度和并行进程数必须是整数。") from exc
        return JobSpec(
            audio=tuple(self.audio),
            images=tuple(self.images),
            assignments=tuple(self.resolve_assignments()),
            output=Path(self.output.get().strip()),
            subtitle=self.subtitle.get(),
            fps=fps,
            width=width,
            workers=workers,
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
        )

    def start(self) -> None:
        if self.running:
            return
        try:
            job = self._snapshot_job()
        except ValueError as exc:
            messagebox.showerror("输入无效", str(exc))
            return
        errors = validate_job(job)
        if errors:
            messagebox.showerror("无法开始", "\n".join(errors))
            return
        self.save()
        self.running = True
        self.start_button.configure(state="disabled")
        self.total_progress.set(1)
        self.status.set("已开始：读取音频时长...")
        self.set_tasks()
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()

    def _run_job(self, job: JobSpec) -> None:
        failed_event_seen = False

        def on_event(event: dict[str, object]) -> None:
            nonlocal failed_event_seen
            failed_event_seen = failed_event_seen or event.get("type") == "job_failed"
            self.events.put(event)

        try:
            RenderEngine(on_event).run(job)
        except Exception as exc:
            if not failed_event_seen:
                self.events.put({"type": "worker_exception", "error": str(exc)})

    def merge_existing(self) -> None:
        if self.running:
            return
        work = filedialog.askdirectory(title="选择 _parallel_work 中间目录")
        if not work:
            return
        work_path = Path(work)
        if not any(path.stem.isdigit() for path in work_path.glob("*.mp4")):
            messagebox.showerror("没有分段", "所选目录中没有 MP4 分段。")
            return
        output = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4")],
        )
        if not output:
            return
        self.running = True
        self.start_button.configure(state="disabled")
        self.status.set("正在合并已有分段...")
        threading.Thread(
            target=self._run_merge,
            args=(work_path, Path(output)),
            daemon=True,
        ).start()

    def _run_merge(self, work: Path, output: Path) -> None:
        try:
            RenderEngine(self.events.put).merge_existing(
                work,
                output,
                self.ffmpeg,
            )
        except Exception as exc:
            self.events.put({"type": "worker_exception", "error": str(exc)})

    def _finish_idle(self) -> None:
        self.running = False
        self.start_button.configure(state="normal")

    def poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                event_type = event.get("type")
                if event_type == "job_started":
                    self.status.set("已开始：读取音频时长...")
                elif event_type == "probe_finished":
                    self.status.set(f"已读取：{Path(str(event['audio'])).name}")
                elif event_type == "task_started":
                    index = int(event["index"])
                    subtitle = "有字幕" if Path(str(event["audio"])).with_name(
                        Path(str(event["audio"])).name + ".vtt"
                    ).is_file() else "无字幕"
                    self.task_table.item(
                        str(index),
                        values=("处理中", "0%", f"{Path(str(event['audio'])).name}  [{subtitle}]"),
                    )
                elif event_type == "subtitle_missing":
                    self.status.set(f"{Path(str(event['audio'])).name} 无字幕，继续生成无字幕片段")
                elif event_type == "task_progress":
                    index = int(event["index"])
                    self.task_table.item(
                        str(index),
                        values=("处理中", "运行中", str(event["progress"])),
                    )
                elif event_type == "task_finished":
                    index = int(event["index"])
                    self.task_table.item(
                        str(index),
                        values=("完成", "100%", Path(str(event["audio"])).name),
                    )
                elif event_type == "task_skipped":
                    index = int(event["index"])
                    self.task_table.item(
                        str(index),
                        values=("跳过", "100%", Path(str(event["audio"])).name),
                    )
                elif event_type == "overall_progress":
                    self.total_progress.set(float(event["progress"]))
                elif event_type == "merge_started":
                    self.status.set("正在合并分段...")
                elif event_type == "job_finished":
                    self._finish_idle()
                    self.total_progress.set(100)
                    self.status.set(f"完成：{event['output']}")
                    messagebox.showinfo("完成", str(event["output"]))
                elif event_type == "merge_finished":
                    self._finish_idle()
                    self.status.set(f"合并完成：{event['output']}")
                    messagebox.showinfo("合并完成", str(event["output"]))
                elif event_type in {"job_failed", "worker_exception"}:
                    self._finish_idle()
                    self.status.set("失败")
                    messagebox.showerror("失败", str(event["error"]))
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

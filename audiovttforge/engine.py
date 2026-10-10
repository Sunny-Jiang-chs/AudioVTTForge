"""Headless rendering engine for AudioVTTForge."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .job import (
    AUDIO_CODECS,
    FONT_OUTLINE_WIDTH_LIMITS,
    FONT_SHADOW_LIMITS,
    FONT_SIZE_LIMITS,
    SUBTITLE_MARGIN_LIMITS,
    JobSpec,
    SUBTITLE_MODES,
)
from .media import find_subtitle, read_subtitle, video_dimensions, write_srt

EventSink = Callable[[dict[str, Any]], None]


class EngineError(RuntimeError):
    """Base class for errors raised by the rendering engine."""


class ValidationError(EngineError):
    """Raised when a job cannot be started."""


class JobCancelled(EngineError):
    """Raised when a render job is cancelled by its owner."""


@dataclass(frozen=True)
class RunResult:
    output: Path
    work: Path
    segments: tuple[Path, ...]
    events: Path
    summary: Path


def _tool_command(tool: Path, arguments: Iterable[str]) -> list[str]:
    if tool.suffix.lower() in {".py", ".pyw"}:
        return [sys.executable, str(tool), *arguments]
    return [str(tool), *arguments]


def _tool_available(tool: Path) -> bool:
    return tool.is_file() or shutil.which(str(tool)) is not None


def _tool_environment(tool: Path) -> dict[str, str]:
    """Return a child environment that can load a tool's neighboring DLLs.

    Windows does not always retain the directory containing FFmpeg's optional
    DLLs in ``PATH`` (notably when the GUI is launched from Explorer or a
    packaged application).  The executable can still be found by its absolute
    path while a child process fails to load one of those DLLs.  Prepending the
    tool directory keeps the headless engine and the GUI on the same reliable
    process setup.
    """
    environment = os.environ.copy()
    try:
        tool_directory = str(tool.expanduser().resolve().parent)
    except OSError:
        tool_directory = str(tool.parent)
    current_path = environment.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    if tool_directory not in path_entries:
        environment["PATH"] = os.pathsep.join([tool_directory, *path_entries])
    return environment


def _event(event_type: str, **payload: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "time": datetime.now(timezone.utc).isoformat(),
        **payload,
    }


def _ass_color(value: str) -> str:
    """Convert a CSS #RRGGBB color to the BGR order used by ASS subtitles."""
    color = value.lstrip("#")
    return f"&H00{color[4:6]}{color[2:4]}{color[0:2]}"


# FFmpeg parses a filter graph in two passes, so a path used as a filter option
# value must be escaped once for the option parser and again for the graph
# parser.  Quoting alone is not enough: an apostrophe in the path closes the
# quote early, after which a comma or a bracket is read as graph syntax.
_FILTER_OPTION_SPECIAL = "\\'[];,:"
_FILTER_GRAPH_SPECIAL = "\\'[];,"


def _escape_filter_value(value: str, specials: str) -> str:
    return "".join(f"\\{char}" if char in specials else char for char in value)


def escape_filter_path(path: Path) -> str:
    """Escape a filesystem path for use as an unquoted FFmpeg filter value.

    Without this a source or output directory such as ``Bob's [final], v2``
    makes FFmpeg fail with a filtergraph parse error.
    """
    posix = path.as_posix()
    return _escape_filter_value(
        _escape_filter_value(posix, _FILTER_OPTION_SPECIAL),
        _FILTER_GRAPH_SPECIAL,
    )


def _log_tail(path: Path, limit: int = 2000) -> str:
    """Read the end of a possibly large FFmpeg log without loading all of it."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > limit * 4:
                stream.seek(-limit * 4, os.SEEK_END)
            data = stream.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()[-limit:]


def concat_line(path: Path) -> str:
    """Quote one segment path for the FFmpeg concat demuxer list.

    The demuxer tokenizes each line like a filtergraph, so an apostrophe in the
    path (``Bob's show``) must close the quote, escape itself and reopen it;
    plain ``'...'`` quoting silently truncates the filename.
    """
    quoted = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{quoted}'"


class EventLog:
    def __init__(self, path: Path, callback: EventSink | None = None) -> None:
        self.path = path
        self.callback = callback
        self._stream = None

    def __enter__(self) -> "EventLog":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("w", encoding="utf-8")
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._stream:
            self._stream.close()
            self._stream = None

    def emit(self, event_type: str, **payload: Any) -> None:
        event = _event(event_type, **payload)
        if self._stream:
            self._stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._stream.flush()
        if self.callback:
            try:
                self.callback(event)
            except Exception:
                pass


def validate_job(job: JobSpec, check_tools: bool = True) -> list[str]:
    errors: list[str] = []
    if not job.audio:
        errors.append("At least one audio file is required.")
    if job.images and len(job.assignments) != len(job.audio):
        errors.append("The number of assignments must match the number of audio files.")
    if job.images and any(index < 0 or index >= len(job.images) for index in job.assignments):
        errors.append("An image assignment is outside the image list.")
    if job.subtitle not in SUBTITLE_MODES:
        errors.append(f"Unsupported subtitle mode: {job.subtitle}")
    if job.fps < 1:
        errors.append("FPS must be a positive integer.")
    if job.width < 2 or job.width % 2:
        errors.append("Width must be a positive even number.")
    if job.workers < 1:
        errors.append("Workers must be a positive integer.")
    if not isinstance(job.font_name, str) or not job.font_name.strip():
        errors.append("Font name must not be empty.")
    elif any(character in job.font_name for character in ",':\\\r\n"):
        errors.append("Font name contains unsupported characters.")
    if job.font_size < FONT_SIZE_LIMITS[0] or job.font_size > FONT_SIZE_LIMITS[1]:
        errors.append(
            f"Font size must be between {FONT_SIZE_LIMITS[0]} and {FONT_SIZE_LIMITS[1]}."
        )
    if not isinstance(job.font_color, str) or re.fullmatch(r"#[0-9A-Fa-f]{6}", job.font_color) is None:
        errors.append("Font color must be a #RRGGBB value.")
    for name, value in (("font_outline_color", job.font_outline_color), ("font_shadow_color", job.font_shadow_color)):
        if not isinstance(value, str) or re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is None:
            errors.append(f"{name} must be a #RRGGBB value.")
    if job.font_outline_width < FONT_OUTLINE_WIDTH_LIMITS[0] or job.font_outline_width > FONT_OUTLINE_WIDTH_LIMITS[1]:
        errors.append(f"Font outline width must be between {FONT_OUTLINE_WIDTH_LIMITS[0]} and {FONT_OUTLINE_WIDTH_LIMITS[1]}.")
    if job.font_shadow < FONT_SHADOW_LIMITS[0] or job.font_shadow > FONT_SHADOW_LIMITS[1]:
        errors.append(f"Font shadow must be between {FONT_SHADOW_LIMITS[0]} and {FONT_SHADOW_LIMITS[1]}.")
    if job.subtitle_margin < SUBTITLE_MARGIN_LIMITS[0] or job.subtitle_margin > SUBTITLE_MARGIN_LIMITS[1]:
        errors.append(f"Subtitle margin must be between {SUBTITLE_MARGIN_LIMITS[0]} and {SUBTITLE_MARGIN_LIMITS[1]}.")
    if job.audio_codec not in AUDIO_CODECS:
        errors.append(f"Unsupported audio codec: {job.audio_codec}")
    for path in job.audio:
        if not path.is_file():
            errors.append(f"Missing audio file: {path}")
    for path in job.images:
        if not path.is_file():
            errors.append(f"Missing image file: {path}")
    if check_tools:
        if not _tool_available(job.ffmpeg):
            errors.append(f"FFmpeg is not available: {job.ffmpeg}")
        if not _tool_available(job.ffprobe):
            errors.append(f"FFprobe is not available: {job.ffprobe}")
    return errors


def plan_job(job: JobSpec) -> dict[str, Any]:
    width, height = video_dimensions(job.width)
    tasks = []
    for index, audio in enumerate(job.audio):
        image = job.images[job.assignments[index]] if index < len(job.assignments) and job.images else None
        tasks.append(
            {
                "index": index,
                "audio": str(audio),
                "image": str(image) if image else None,
                "subtitle": str(find_subtitle(audio)) if find_subtitle(audio) else None,
                "segment": f"{index:04d}.mp4",
                "duration": "probe-at-run-time",
            }
        )
    return {
        "valid": not validate_job(job, check_tools=False),
        "errors": validate_job(job, check_tools=False),
        "output": str(job.output),
        "work": str(job.output.parent / f".{job.output.stem}_parallel_work"),
        "subtitle": job.subtitle,
        "font_name": job.font_name,
        "font_size": job.font_size,
        "font_color": job.font_color,
        "font_outline_color": job.font_outline_color,
        "font_outline_width": job.font_outline_width,
        "font_shadow_color": job.font_shadow_color,
        "font_shadow": job.font_shadow,
        "subtitle_margin": job.subtitle_margin,
        "audio_codec": job.audio_codec,
        "fps": job.fps,
        "video": {"width": width, "height": height},
        "workers": min(job.workers, len(job.audio)) if job.audio else 0,
        "tasks": tasks,
    }


class RenderEngine:
    def __init__(
        self,
        callback: EventSink | None = None,
        cancel_event: threading.Event | None = None,
        process_callback: Callable[[subprocess.Popen[Any], bool], None] | None = None,
    ) -> None:
        self.callback = callback
        self.cancel_event = cancel_event or threading.Event()
        self.process_callback = process_callback

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled("Render job was cancelled")

    def _track_process(self, process: subprocess.Popen[Any], active: bool) -> None:
        if self.process_callback:
            self.process_callback(process, active)

    def _render_command(
        self,
        job: JobSpec,
        index: int,
        duration: float,
        segment: Path,
        subtitle_path: Path | None,
    ) -> list[str]:
        width, height = video_dimensions(job.width)
        audio = job.audio[index]
        image = job.images[job.assignments[index]] if job.images else None
        fps = job.fps
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:"
            f"color=black,fps={fps},setpts=N/FRAME_RATE/TB,setsar=1"
        )
        args = ["-hide_banner", "-y"]
        if image is None:
            args += [
                "-f", "lavfi",
                "-i", f"color=c=black:s={width}x{height}:r={fps}:d={duration:.3f}",
            ]
        else:
            args += [
                "-loop", "1",
                "-framerate", str(fps),
                "-t", f"{duration:.3f}",
                "-i", str(image),
            ]
        args += ["-i", str(audio)]
        subtitle_file = subtitle_path
        has_subtitle = subtitle_file is not None and subtitle_file.is_file()
        if job.subtitle == "embedded" and has_subtitle:
            args += ["-i", str(subtitle_file)]
        args += [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
        if job.subtitle == "burnin" and has_subtitle:
            video_filter += (
                f",subtitles={escape_filter_path(subtitle_file)}:original_size={width}x{height}:force_style="
                f"'PlayResX={width},PlayResY={height},FontName={job.font_name},FontSize={job.font_size},"
                f"PrimaryColour={_ass_color(job.font_color)},"
                f"OutlineColour={_ass_color(job.font_outline_color)},Outline={job.font_outline_width},"
                f"ShadowColour={_ass_color(job.font_shadow_color)},Shadow={job.font_shadow},"
                f"Alignment=2,MarginV={job.subtitle_margin}'"
            )
        elif job.subtitle == "embedded" and has_subtitle:
            args += ["-map", "2:0"]
        args += [
            "-vf",
            video_filter,
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-t",
            f"{duration:.3f}",
        ]
        audio_options = ["-c:a", "alac"] if job.audio_codec == "alac" else ["-c:a", "aac", "-b:a", "512k"]
        args[args.index("-ar"):args.index("-ar")] = audio_options
        if job.subtitle == "embedded" and has_subtitle:
            args += ["-c:s", "mov_text"]
        args += ["-movflags", "+faststart", str(segment)]
        return _tool_command(job.ffmpeg, args)

    def _probe(self, job: JobSpec, audio: Path, events: EventLog) -> float:
        self._check_cancelled()
        args = _tool_command(
            job.ffprobe,
            [
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=duration",
                "-of",
                "json",
                str(audio),
            ],
        )
        events.emit("probe_started", audio=str(audio), command=args)
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(job.ffprobe.parent) if job.ffprobe.parent != Path(".") else None,
                env=_tool_environment(job.ffprobe),
            )
            self._track_process(process, True)
            stdout, stderr = process.communicate()
            self._check_cancelled()
            if process.returncode != 0:
                raise EngineError(f"FFprobe failed for {audio}: {(stderr or stdout).strip()}")
            data = json.loads(stdout)
            duration = float(data["streams"][0]["duration"])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("duration is not positive and finite")
        except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._check_cancelled()
            raise EngineError(f"FFprobe failed for {audio}: {exc}") from exc
        finally:
            if process is not None:
                self._track_process(process, False)
        events.emit("probe_finished", audio=str(audio), duration=duration)
        return duration

    def _prepare_subtitle(
        self,
        job: JobSpec,
        index: int,
        audio: Path,
        duration: float,
        work: Path,
        events: EventLog,
    ) -> Path | None:
        """Return the subtitle file the render should use, if any.

        LRC always gets converted to SRT.  A subtitle file that exists but holds
        no cues is reported and skipped instead of failing the whole render.
        """
        if job.subtitle == "none":
            return None
        source = find_subtitle(audio)
        if source is None:
            events.emit(
                "subtitle_missing",
                index=index,
                audio=str(audio),
                subtitle=str(audio.with_name(audio.stem + ".vtt")),
            )
            return None
        try:
            cues = read_subtitle(source, round(duration * 1000))
        except ValueError as exc:
            raise EngineError(f"Subtitle could not be parsed: {source}: {exc}") from exc
        if not cues:
            events.emit(
                "subtitle_empty",
                index=index,
                audio=str(audio),
                subtitle=str(source),
            )
            return None
        if job.subtitle == "burnin" or source.suffix.lower() == ".lrc":
            srt_path = work / f"{index:04d}.srt"
            write_srt(cues, srt_path)
            return srt_path
        return source

    def _render_one(
        self,
        job: JobSpec,
        index: int,
        duration: float,
        work: Path,
        events: EventLog,
        resume: bool,
    ) -> Path:
        self._check_cancelled()
        audio = job.audio[index]
        segment = work / f"{index:04d}.mp4"
        if resume and segment.is_file() and segment.stat().st_size > 0:
            events.emit("task_skipped", index=index, audio=str(audio), segment=str(segment))
            return segment

        subtitle_path = self._prepare_subtitle(job, index, audio, duration, work, events)
        command = self._render_command(job, index, duration, segment, subtitle_path)
        log_path = work / f"{index:04d}.log"
        events.emit("task_started", index=index, audio=str(audio), command=command)
        try:
            with log_path.open("w", encoding="utf-8") as log:
                self._check_cancelled()
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(job.ffmpeg.parent) if job.ffmpeg.parent != Path(".") else None,
                    env=_tool_environment(job.ffmpeg),
                )
                self._track_process(process, True)
                try:
                    assert process.stdout is not None
                    for line in process.stdout:
                        log.write(line)
                        line = line.strip()
                        if line:
                            events.emit(
                                "task_progress",
                                index=index,
                                audio=str(audio),
                                progress=line[-160:],
                            )
                    return_code = process.wait()
                finally:
                    self._track_process(process, False)
        except OSError as exc:
            self._check_cancelled()
            raise EngineError(f"Could not start FFmpeg for {audio}: {exc}") from exc
        self._check_cancelled()
        if return_code != 0:
            details = _log_tail(log_path)
            raise EngineError(
                f"FFmpeg failed for {audio}; see {log_path}\n{details}"
            )
        if not segment.is_file():
            raise EngineError(f"FFmpeg did not create the expected segment: {segment}")
        events.emit("task_finished", index=index, audio=str(audio), segment=str(segment))
        return segment

    def _merge(
        self,
        ffmpeg: Path,
        segments: list[Path],
        work: Path,
        output: Path,
        events: EventLog,
        log_name: str = "join.log",
    ) -> None:
        self._check_cancelled()
        concat = work / "segments.txt"
        concat.write_text(
            "\n".join(concat_line(path) for path in segments) + "\n",
            encoding="utf-8",
        )
        merge_output = work / "merged.mp4"
        log_path = work / log_name
        command = self._merge_command(ffmpeg, concat, merge_output)
        events.emit("merge_started", command=command, output=str(output))
        process: subprocess.Popen[Any] | None = None
        try:
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=str(ffmpeg.parent) if ffmpeg.parent != Path(".") else None,
                    env=_tool_environment(ffmpeg),
                )
                self._track_process(process, True)
                try:
                    return_code = process.wait()
                finally:
                    self._track_process(process, False)
        except OSError as exc:
            self._check_cancelled()
            raise EngineError(f"Could not start merge process; see {log_path}") from exc
        self._check_cancelled()
        if return_code != 0:
            details = _log_tail(log_path)
            raise EngineError(f"Merge failed; see {log_path}\n{details}")
        if not merge_output.is_file():
            raise EngineError(f"Merge did not create the expected output: {merge_output}")
        try:
            os.replace(merge_output, output)
        except OSError as exc:
            raise EngineError(
                f"Could not replace output file: {output} "
                f"(errno={exc.errno}, winerror={getattr(exc, 'winerror', None)}, "
                f"source={str(merge_output)!r}, target={str(output)!r})"
            ) from exc
        events.emit("merge_finished", output=str(output))

    @staticmethod
    def _merge_command(ffmpeg: Path, concat: Path, output: Path) -> list[str]:
        return _tool_command(
            ffmpeg,
            [
                "-hide_banner",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat),
                "-map",
                "0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ],
        )

    def run(self, job: JobSpec, keep_work: bool = False, resume: bool = False) -> RunResult:
        self._check_cancelled()
        errors = validate_job(job)
        if errors:
            raise ValidationError("\n".join(errors))
        try:
            job.output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EngineError(
                f"Output directory is not writable: {job.output.parent}. "
                "Choose a user-writable output folder. Temporary files stay beside the output and are cleaned after success."
            ) from exc
        work = job.output.parent / f".{job.output.stem}_parallel_work"
        work.mkdir(parents=True, exist_ok=True)
        events_path = job.output.parent / f".{job.output.stem}.events.jsonl"
        summary_path = job.output.parent / f".{job.output.stem}.summary.json"
        (work / "job.json").write_text(
            json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        segments: list[Path] = [work / f"{index:04d}.mp4" for index in range(len(job.audio))]
        result_data: dict[str, Any] = {
            "output": str(job.output),
            "work": str(work),
            "events": str(events_path),
            "success": False,
        }
        try:
            with EventLog(events_path, self.callback) as events:
                try:
                    events.emit(
                        "job_started",
                        output=str(job.output),
                        work=str(work),
                        task_count=len(job.audio),
                        resume=resume,
                    )
                    durations = [
                        self._probe(job, audio, events)
                        for audio in job.audio
                    ]
                    workers = min(job.workers, len(job.audio))
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures: dict[Future[Path], int] = {
                            pool.submit(
                                self._render_one,
                                job,
                                index,
                                durations[index],
                                work,
                                events,
                                resume,
                            ): index
                            for index in range(len(job.audio))
                        }
                        completed = 0
                        for future in as_completed(futures):
                            self._check_cancelled()
                            index = futures[future]
                            segments[index] = future.result()
                            completed += 1
                            events.emit(
                                "overall_progress",
                                progress=10 + completed / len(segments) * 80,
                                completed=completed,
                                total=len(segments),
                            )
                    self._merge(job.ffmpeg, segments, work, job.output, events)
                    self._check_cancelled()
                    events.emit("job_finished", output=str(job.output))
                    result_data.update({"success": True, "segments": [str(path) for path in segments]})
                except JobCancelled as exc:
                    result_data.update({"error": str(exc), "segments": [str(path) for path in segments]})
                    events.emit("job_cancelled", output=str(job.output), work=str(work))
                    raise
                except Exception as exc:
                    result_data.update({"error": str(exc), "segments": [str(path) for path in segments]})
                    events.emit("job_failed", error=str(exc), work=str(work))
                    raise
        except Exception as exc:
            result_data.setdefault("error", str(exc))
            raise
        finally:
            summary_path.write_text(
                json.dumps(result_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
        return RunResult(job.output, work, tuple(segments), events_path, summary_path)

    def merge_existing(
        self,
        work: Path,
        output: Path,
        ffmpeg: Path,
        callback: EventSink | None = None,
    ) -> Path:
        segments = sorted(path for path in work.glob("*.mp4") if path.stem.isdigit())
        if not segments:
            raise ValidationError(f"No numbered MP4 segments found in {work}")
        output.parent.mkdir(parents=True, exist_ok=True)
        events_path = work / "merge_events.jsonl"
        with EventLog(events_path, callback) as events:
            self._merge(ffmpeg, segments, work, output, events, "join_recovery.log")
        return output


def inspect_tool(tool: Path) -> dict[str, Any]:
    available = _tool_available(tool)
    report: dict[str, Any] = {"path": str(tool), "available": available}
    if not available:
        return report
    try:
        result = subprocess.run(
            _tool_command(tool, ["-version"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env=_tool_environment(tool),
        )
        first_line = (result.stdout or result.stderr).splitlines()
        report["version"] = first_line[0] if first_line else ""
        report["returncode"] = result.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
    return report

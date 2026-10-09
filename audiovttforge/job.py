"""Serializable job definitions and tool resolution."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .media import automatic_image_index

DEFAULT_FFMPEG = Path(
    r"D:\tools\ffmpeg\ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe"
)
DEFAULT_FFPROBE = Path(
    r"D:\tools\ffmpeg\ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe"
)
SUBTITLE_MODES = {"burnin", "embedded", "none"}
# Domain limits shared by validation, the REST layer and the capabilities resource.
WORKER_LIMITS = (1, 10)
FONT_SIZE_LIMITS = (8, 144)


def _resolve_path(value: str | os.PathLike[str], base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def resolve_tool(
    explicit: str | os.PathLike[str] | None,
    environment_name: str,
    default: Path,
    executable_name: str,
) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from_environment = os.environ.get(environment_name)
    if from_environment:
        return Path(from_environment).expanduser()
    if default.is_file():
        return default
    found = shutil.which(executable_name)
    return Path(found) if found else default


def _resolve_job_tool(
    value: str | os.PathLike[str] | None,
    base_dir: Path,
    environment_name: str,
    default: Path,
    executable_name: str,
) -> Path:
    if value:
        return _resolve_path(value, base_dir)
    return resolve_tool(None, environment_name, default, executable_name)


def default_ffmpeg() -> Path:
    return resolve_tool(None, "AUDIOVTTFORGE_FFMPEG", DEFAULT_FFMPEG, "ffmpeg")


def default_ffprobe() -> Path:
    return resolve_tool(None, "AUDIOVTTFORGE_FFPROBE", DEFAULT_FFPROBE, "ffprobe")


@dataclass(frozen=True)
class JobSpec:
    audio: tuple[Path, ...]
    images: tuple[Path, ...]
    assignments: tuple[int, ...]
    output: Path
    subtitle: str = "burnin"
    fps: int = 2
    width: int = 1920
    workers: int = 2
    font_name: str = "Microsoft YaHei"
    font_size: int = 42
    font_color: str = "#FFFFFF"
    ffmpeg: Path = DEFAULT_FFMPEG
    ffprobe: Path = DEFAULT_FFPROBE

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path | None = None) -> "JobSpec":
        root = (base_dir or Path.cwd()).resolve()
        raw_audio = data.get("audio", [])
        raw_images = data.get("images", [])
        if not isinstance(raw_audio, list) or not isinstance(raw_images, list):
            raise ValueError("'audio' and 'images' must be arrays")
        audio = tuple(_resolve_path(item, root) for item in raw_audio)
        images = tuple(_resolve_path(item, root) for item in raw_images)
        if "output" not in data:
            raise ValueError("'output' is required")

        raw_assignments = data.get("assignments")
        if raw_assignments is None:
            assignments = tuple(
                automatic_image_index(index, len(audio), len(images))
                for index in range(len(audio))
            ) if audio and images else ()
        elif isinstance(raw_assignments, list):
            assignments = tuple(int(item) for item in raw_assignments)
        else:
            raise ValueError("'assignments' must be an array")

        return cls(
            audio=audio,
            images=images,
            assignments=assignments,
            output=_resolve_path(data["output"], root),
            subtitle=str(data.get("subtitle", "burnin")),
            fps=int(data.get("fps", 2)),
            width=int(data.get("width", 1920)),
            workers=int(data.get("workers", 2)),
            font_name=str(data.get("font_name", "Microsoft YaHei")),
            font_size=int(data.get("font_size", 42)),
            font_color=str(data.get("font_color", "#FFFFFF")),
            ffmpeg=_resolve_job_tool(
                data.get("ffmpeg"),
                root,
                "AUDIOVTTFORGE_FFMPEG",
                DEFAULT_FFMPEG,
                "ffmpeg",
            ),
            ffprobe=_resolve_job_tool(
                data.get("ffprobe"),
                root,
                "AUDIOVTTFORGE_FFPROBE",
                DEFAULT_FFPROBE,
                "ffprobe",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "audio": [str(path) for path in self.audio],
            "images": [str(path) for path in self.images],
            "assignments": list(self.assignments),
            "output": str(self.output),
            "subtitle": self.subtitle,
            "fps": self.fps,
            "width": self.width,
            "workers": self.workers,
            "font_name": self.font_name,
            "font_size": self.font_size,
            "font_color": self.font_color,
            "ffmpeg": str(self.ffmpeg),
            "ffprobe": str(self.ffprobe),
        }


def load_job(path: Path) -> JobSpec:
    import json

    job_path = path.expanduser().resolve()
    data = json.loads(job_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Job file must contain a JSON object")
    return JobSpec.from_dict(data, job_path.parent)

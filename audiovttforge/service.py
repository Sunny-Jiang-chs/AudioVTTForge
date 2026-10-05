"""Application services for the local REST API.

The service owns source-directory scans, compatibility uploads, and
asynchronous render jobs.  The existing ``RenderEngine`` remains responsible
for media processing; this module only maps HTTP-friendly resource data to
``JobSpec`` and records its events.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import RenderEngine, validate_job
from .job import JobSpec


class ServiceError(RuntimeError):
    """Base error raised for invalid service resource requests."""


class NotFoundError(ServiceError):
    """Requested upload or job does not exist."""


class RequestValidationError(ServiceError):
    """Request data cannot be converted into a valid job."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


def default_data_root() -> Path:
    configured = os.environ.get("AUDIOVTTFORGE_WEB_ROOT")
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "AudioVTTForge" / "web"
    return Path.home() / ".audiovttforge" / "web"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str, fallback: str) -> str:
    name = Path(value).name.strip()
    if not name or name in {".", ".."}:
        return fallback
    return name


def _natural_sort_key(value: str) -> tuple[tuple[int, object], ...]:
    """Sort names like 01, 02, 10 instead of 01, 10, 02."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )


@dataclass(frozen=True)
class UploadRecord:
    upload_id: str
    name: str
    path: Path
    size: int
    kind: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.upload_id,
            "name": self.name,
            "size": self.size,
            "kind": self.kind,
            "created_at": self.created_at,
        }


class UploadStore:
    """Store browser uploads outside the source checkout."""

    AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg"}
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, UploadRecord] = {}
        self._lock = threading.RLock()

    @classmethod
    def kind_for_name(cls, name: str) -> str:
        suffix = Path(name).suffix.lower()
        if suffix in cls.AUDIO_EXTENSIONS:
            return "audio"
        if suffix in cls.IMAGE_EXTENSIONS:
            return "image"
        if suffix == ".vtt":
            return "subtitle"
        return "other"

    def save(self, name: str, content: bytes) -> UploadRecord:
        safe_name = _safe_name(name, "upload.bin")
        upload_id = uuid.uuid4().hex
        directory = self.root / upload_id
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / safe_name
        path.write_bytes(content)
        record = UploadRecord(
            upload_id=upload_id,
            name=safe_name,
            path=path,
            size=len(content),
            kind=self.kind_for_name(safe_name),
            created_at=_now(),
        )
        with self._lock:
            self._records[upload_id] = record
        return record

    def get(self, upload_id: str) -> UploadRecord:
        with self._lock:
            record = self._records.get(upload_id)
        if record is None:
            raise NotFoundError(f"Upload not found: {upload_id}")
        return record


@dataclass(frozen=True)
class SourceScan:
    """A read-only preview of a user-provided local source directory."""

    directory: Path
    audio: tuple[Path, ...]
    images: tuple[Path, ...]
    subtitles: tuple[Path, ...]

    @property
    def warnings(self) -> list[str]:
        warnings: list[str] = []
        if not self.audio:
            warnings.append("未找到音频文件。")
        if not self.images:
            warnings.append("未找到图片文件。")
        subtitles = {path.name.casefold() for path in self.subtitles}
        for audio in self.audio:
            expected = f"{audio.name}.vtt"
            if expected.casefold() not in subtitles:
                warnings.append(f"缺少字幕：{expected}")
        return warnings

    @property
    def ready(self) -> bool:
        return bool(self.audio and self.images)

    def to_dict(self) -> dict[str, Any]:
        subtitle_names = {path.name.casefold() for path in self.subtitles}

        def describe(path: Path) -> dict[str, Any]:
            return {"name": path.name, "size": path.stat().st_size}

        audio = [
            {
                **describe(path),
                "subtitle": f"{path.name}.vtt" if f"{path.name}.vtt".casefold() in subtitle_names else None,
            }
            for path in self.audio
        ]
        return {
            "source_dir": str(self.directory),
            "source_name": self.directory.name or str(self.directory),
            "audio": audio,
            "images": [describe(path) for path in self.images],
            "subtitles": [describe(path) for path in self.subtitles],
            "counts": {
                "audio": len(self.audio),
                "images": len(self.images),
                "subtitles": len(self.subtitles),
                "total": len(self.audio) + len(self.images) + len(self.subtitles),
            },
            "warnings": self.warnings,
            "ready": self.ready,
        }


def scan_source_directory(value: str | os.PathLike[str]) -> SourceScan:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise RequestValidationError("'path' must be a non-empty directory path")
    try:
        directory = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise RequestValidationError(f"Source directory could not be opened: {value}") from exc
    if not directory.is_dir():
        raise RequestValidationError(f"Source path is not a directory: {directory}")
    try:
        entries = [path for path in directory.iterdir() if path.is_file()]
    except OSError as exc:
        raise RequestValidationError(f"Source directory could not be read: {directory}") from exc

    audio = tuple(
        sorted(
            (path for path in entries if path.suffix.lower() in UploadStore.AUDIO_EXTENSIONS),
            key=lambda path: _natural_sort_key(path.name),
        )
    )
    images = tuple(
        sorted(
            (path for path in entries if path.suffix.lower() in UploadStore.IMAGE_EXTENSIONS),
            key=lambda path: _natural_sort_key(path.name),
        )
    )
    subtitles = tuple(
        sorted(
            (path for path in entries if path.suffix.lower() == ".vtt"),
            key=lambda path: _natural_sort_key(path.name),
        )
    )
    return SourceScan(directory, audio, images, subtitles)


@dataclass
class JobRecord:
    job_id: str
    created_at: str
    job: JobSpec
    root: Path
    source_dir: Path | None = None
    state: str = "queued"
    progress: float = 0.0
    current_task: str | None = None
    error: str | None = None
    result_output: Path | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.job_id,
            "state": self.state,
            "created_at": self.created_at,
            "progress": round(max(0.0, min(100.0, self.progress)), 1),
            "current_task": self.current_task,
            "error": self.error,
            "output_name": self.job.output.name,
            "source_dir": str(self.source_dir) if self.source_dir else None,
            "output_ready": bool(self.result_output and self.result_output.is_file()),
            "event_count": len(self.events),
            "events_url": f"/api/v1/jobs/{self.job_id}/events",
            "download_url": f"/api/v1/jobs/{self.job_id}/download",
        }


class JobManager:
    """Create and run render jobs while exposing stable REST resource state."""

    def __init__(
        self,
        root: Path | None = None,
        max_concurrent_jobs: int = 1,
    ) -> None:
        self.root = (root or default_data_root()).expanduser().resolve()
        self.uploads = UploadStore(self.root / "uploads")
        self.jobs_root = self.root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_concurrent_jobs))

    def shutdown(self, wait: bool = False, cancel_futures: bool = True) -> None:
        """Release worker resources when the local API server exits."""
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def upload(self, name: str, content: bytes) -> UploadRecord:
        if not content:
            raise RequestValidationError(f"Upload is empty: {name}")
        return self.uploads.save(name, content)

    def scan(self, source_dir: str | os.PathLike[str]) -> SourceScan:
        return scan_source_directory(source_dir)

    def _copy_uploads(self, job_id: str, upload_ids: list[str]) -> tuple[Path, dict[str, Path]]:
        input_root = self.jobs_root / job_id / "inputs"
        input_root.mkdir(parents=True, exist_ok=False)
        copied: dict[str, Path] = {}
        used_names: set[str] = set()
        for upload_id in upload_ids:
            record = self.uploads.get(str(upload_id))
            name = record.name
            if name.lower() in used_names:
                raise RequestValidationError(f"Duplicate uploaded filename: {name}")
            used_names.add(name.lower())
            destination = input_root / name
            shutil.copy2(record.path, destination)
            copied[record.upload_id] = destination
        return input_root, copied

    @staticmethod
    def _string_list(payload: dict[str, Any], key: str) -> list[str]:
        value = payload.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise RequestValidationError(f"'{key}' must be an array of upload ids")
        return value

    def _build_job(self, job_id: str, payload: dict[str, Any]) -> tuple[JobSpec, Path, Path | None]:
        source_value = payload.get("source_dir")
        source_dir: Path | None = None
        if source_value is not None:
            scan = self.scan(source_value)
            source_dir = scan.directory
            input_root = scan.directory
            audio_paths = [str(path) for path in scan.audio]
            image_paths = [str(path) for path in scan.images]
        else:
            audio_ids = self._string_list(payload, "audio")
            image_ids = self._string_list(payload, "images")
            all_ids = self._string_list(payload, "files")
            if not all_ids:
                all_ids = list(dict.fromkeys([*audio_ids, *image_ids, *self._string_list(payload, "subtitles")]))
            if not all_ids:
                raise RequestValidationError("At least one uploaded file is required")
            unknown = [item for item in [*audio_ids, *image_ids] if item not in all_ids]
            if unknown:
                raise RequestValidationError("Audio and image uploads must be included in 'files'")

            input_root, copied = self._copy_uploads(job_id, all_ids)
            audio_paths = [str(copied[item]) for item in audio_ids]
            image_paths = [str(copied[item]) for item in image_ids]

        output_name = _safe_name(str(payload.get("output_name", "result.mp4")), "result.mp4")
        if Path(output_name).suffix.lower() != ".mp4":
            output_name += ".mp4"
        try:
            workers = int(payload.get("workers", 2))
        except (TypeError, ValueError) as exc:
            raise RequestValidationError("并行进程数必须是 1 到 10 之间的整数") from exc
        if not 1 <= workers <= 10:
            raise RequestValidationError("并行进程数必须是 1 到 10 之间的整数")
        data: dict[str, Any] = {
            "audio": audio_paths,
            "images": image_paths,
            "output": str(self.jobs_root / job_id / "output" / output_name),
            "subtitle": payload.get("subtitle", "burnin"),
            "fps": payload.get("fps", 2),
            "width": payload.get("width", 1920),
            "workers": workers,
            "font_name": payload.get("font_name", "Microsoft YaHei"),
            "font_size": payload.get("font_size", 42),
            "font_color": payload.get("font_color", "#FFFFFF"),
        }
        if "assignments" in payload:
            data["assignments"] = payload["assignments"]
        for key in ("ffmpeg", "ffprobe"):
            if payload.get(key):
                data[key] = payload[key]
        job = JobSpec.from_dict(data, input_root)
        return job, input_root, source_dir

    def submit(self, payload: dict[str, Any]) -> JobRecord:
        if not isinstance(payload, dict):
            raise RequestValidationError("Job body must be a JSON object")
        job_id = uuid.uuid4().hex
        job_root = self.jobs_root / job_id
        job_root.mkdir(parents=True, exist_ok=False)
        try:
            job, _input_root, source_dir = self._build_job(job_id, payload)
            errors = validate_job(job)
            if errors:
                raise RequestValidationError("Job validation failed", errors)
        except RequestValidationError:
            shutil.rmtree(job_root, ignore_errors=True)
            raise
        except (TypeError, ValueError) as exc:
            shutil.rmtree(job_root, ignore_errors=True)
            raise RequestValidationError(str(exc)) from exc
        except Exception:
            shutil.rmtree(job_root, ignore_errors=True)
            raise

        record = JobRecord(job_id=job_id, created_at=_now(), job=job, root=job_root, source_dir=source_dir)
        with self._lock:
            self._jobs[job_id] = record
            self._append_event(record, "job_queued", output_name=job.output.name)
        self._executor.submit(self._run, job_id)
        return record

    def _append_event(self, record: JobRecord, event_type: str, **payload: Any) -> None:
        event = {"type": event_type, "time": _now(), **payload}
        event["seq"] = len(record.events) + 1
        record.events.append(event)

    def _on_engine_event(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            self._append_event(record, str(event.get("type", "event")), **{
                key: value for key, value in event.items() if key not in {"type", "time"}
            })
            event_type = event.get("type")
            if event_type == "overall_progress":
                record.progress = float(event.get("progress", record.progress))
            elif event_type == "task_started":
                record.current_task = Path(str(event.get("audio", ""))).name or None
            elif event_type == "task_finished":
                record.current_task = None
            elif event_type == "job_finished":
                record.progress = 100.0
                record.current_task = None
            elif event_type == "job_failed":
                record.error = str(event.get("error", "Render failed"))

    def _run(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            record.state = "running"
        try:
            result = RenderEngine(lambda event: self._on_engine_event(job_id, event)).run(record.job)
        except Exception as exc:
            with self._lock:
                record.state = "failed"
                record.error = str(exc)
            return
        with self._lock:
            record.state = "succeeded"
            record.result_output = result.output
            record.progress = 100.0

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            record = self._jobs.get(job_id)
        if record is None:
            raise NotFoundError(f"Job not found: {job_id}")
        return record

    def events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        record = self.get(job_id)
        with self._lock:
            return [dict(event) for event in record.events if int(event["seq"]) > after]

    def output_path(self, job_id: str) -> Path:
        record = self.get(job_id)
        if record.state != "succeeded" or not record.result_output or not record.result_output.is_file():
            raise ServiceError("Job output is not ready")
        return record.result_output

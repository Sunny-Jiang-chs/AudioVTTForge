import os
import time
from pathlib import Path

import pytest

from audiovttforge.engine import ValidationError
from audiovttforge.service import (
    MAX_RETAINED_PROGRESS_EVENTS,
    JobManager,
    NotFoundError,
    RequestValidationError,
    _suggested_output_name,
    capabilities,
    scan_source_directory,
)


FAKE_TOOL = """\
import json
import pathlib
import sys

args = sys.argv[1:]
if "-show_entries" in args:
    print(json.dumps({"streams": [{"duration": "1.250"}]}))
    raise SystemExit(0)
output = pathlib.Path(args[-1])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b"fake mp4")
print("frame=1 time=00:00:01.250")
"""

SLOW_TOOL = """\
import json
import pathlib
import sys
import time

args = sys.argv[1:]
if "-show_entries" in args:
    print(json.dumps({"streams": [{"duration": "1.250"}]}))
    raise SystemExit(0)
output = pathlib.Path(args[-1])
output.parent.mkdir(parents=True, exist_ok=True)
output.with_suffix(".started").write_text("started")
while True:
    time.sleep(1)
"""

NOISY_TOOL = """\
import json
import pathlib
import sys

args = sys.argv[1:]
if "-show_entries" in args:
    print(json.dumps({"streams": [{"duration": "1.250"}]}))
    raise SystemExit(0)
for index in range(1500):
    print(f"frame={index} time=00:00:01.250")
output = pathlib.Path(args[-1])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b"fake mp4")
"""

FAILING_TOOL = """\
import json
import sys

args = sys.argv[1:]
if "-show_entries" in args:
    print(json.dumps({"streams": [{"duration": "1.250"}]}))
    raise SystemExit(0)
print("forced render failure")
raise SystemExit(2)
"""


def wait_for_state(manager: JobManager, job_id: str, state: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if manager.get(job_id).state == state:
            return
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {state}: {manager.get(job_id).to_dict()}")


def test_job_manager_runs_upload_backed_job(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    manager = JobManager(tmp_path / "data")
    audio = manager.upload("01.wav", b"audio")
    subtitle = manager.upload("01.wav.vtt", b"WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n")
    image = manager.upload("cover.png", b"image")

    record = manager.submit(
        {
            "files": [audio.upload_id, subtitle.upload_id, image.upload_id],
            "audio": [audio.upload_id],
            "images": [image.upload_id],
            "output_name": "browser-result.mp4",
            "workers": 1,
            "ffmpeg": str(tool),
            "ffprobe": str(tool),
        }
    )

    wait_for_state(manager, record.job_id, "succeeded")
    current = manager.get(record.job_id)
    assert current.result_output is not None and current.result_output.is_file()
    assert current.result_output.parent == manager.outputs_root
    assert not current.root.exists()
    assert current.to_dict()["output_path"] == str(current.result_output)
    snapshot = manager.describe(record.job_id)
    assert snapshot["state"] == "succeeded"
    assert snapshot["output_ready"] is True
    assert snapshot["output_path"] == str(current.result_output)
    events = manager.events(record.job_id)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert events[0]["type"] == "job_queued"
    assert events[-1]["type"] == "job_finished"
    assert manager.events(record.job_id, events[2]["seq"])[0]["seq"] == events[2]["seq"] + 1
    manager.shutdown()


def test_job_manager_rejects_missing_upload_reference(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "data")

    assert manager.upload("01.lrc", b"[00:01.00]line").kind == "subtitle"

    try:
        manager.submit({"files": ["missing"], "audio": ["missing"], "images": []})
    except NotFoundError as exc:
        assert "Upload not found" in str(exc)
    else:
        raise AssertionError("missing upload should be rejected")
    manager.shutdown()


def test_source_scan_uses_natural_order_and_reports_missing_subtitles(tmp_path: Path) -> None:
    source = tmp_path / "episode01"
    source.mkdir()
    for name in ("10.wav", "02.wav", "01.wav"):
        (source / name).write_bytes(b"audio")
    (source / "01.wav.vtt").write_text("WEBVTT\n", encoding="utf-8")
    (source / "cover.png").write_bytes(b"image")
    (source / "2.png").write_bytes(b"image")

    scan = scan_source_directory(source)

    assert [path.name for path in scan.audio] == ["01.wav", "02.wav", "10.wav"]
    assert [path.name for path in scan.images] == ["2.png", "cover.png"]
    payload = scan.to_dict()
    assert payload["audio"][0]["subtitle"] == "01.wav.vtt"
    assert payload["audio"][1]["subtitle"] is None
    # The default image mapping is computed server-side so the browser never has
    # to re-implement it.
    assert [item["automatic_image"] for item in payload["audio"]] == [0, 0, 1]
    assert payload["audio"][2]["automatic_image_name"] == "cover.png"
    assert any(warning.startswith("缺少字幕：02.wav") for warning in payload["warnings"])
    assert payload["ready"] is True
    assert payload["suggested_output_name"] == _suggested_output_name(source.resolve())


def test_job_manager_runs_source_directory_job(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.wav").write_bytes(b"audio")
    (source / "01.wav.vtt").write_text("WEBVTT\n", encoding="utf-8")
    (source / "cover.png").write_bytes(b"image")
    (source / "alternate.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")

    record = manager.submit(
        {
            "source_dir": str(source),
            "assignments": [1],
            "workers": 1,
            "font_name": "Arial",
            "font_size": 56,
            "font_color": "#12ABEF",
            "ffmpeg": str(tool),
            "ffprobe": str(tool),
        }
    )

    wait_for_state(manager, record.job_id, "succeeded")
    current = manager.get(record.job_id)
    assert current.source_dir == source.resolve()
    assert current.job.assignments == (1,)
    assert current.job.font_name == "Arial"
    assert current.job.font_size == 56
    assert current.job.font_color == "#12ABEF"
    assert current.job.output.name == _suggested_output_name(source.resolve())
    assert current.result_output == source / _suggested_output_name(source.resolve())
    assert current.result_output.is_file()
    assert not current.root.exists()
    manager.shutdown()


def test_source_job_publishes_to_requested_output_directory(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.mp3").write_bytes(b"audio")
    (source / "cover.png").write_bytes(b"image")
    destination = tmp_path / "exports"
    manager = JobManager(tmp_path / "data")

    record = manager.submit(
        {
            "source_dir": str(source),
            "output_dir": str(destination),
            "output_name": "custom-name.mp4",
            "workers": 1,
            "ffmpeg": str(tool),
            "ffprobe": str(tool),
        }
    )

    wait_for_state(manager, record.job_id, "succeeded")
    current = manager.get(record.job_id)
    assert current.result_output == destination / "custom-name.mp4"
    assert current.result_output.read_bytes() == b"fake mp4"
    assert not current.root.exists()
    assert manager.events(record.job_id)[-1]["type"] == "job_finished"
    assert manager.events(record.job_id)[-1]["output"] == str(current.result_output)
    manager.shutdown()


def test_publish_failure_keeps_render_cache_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.wav").write_bytes(b"audio")
    (source / "cover.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")

    def fail_publish(*_args: object) -> Path:
        raise OSError("destination unavailable")

    monkeypatch.setattr(manager, "_publish_output", fail_publish)
    record = manager.submit(
        {
            "source_dir": str(source),
            "workers": 1,
            "ffmpeg": str(tool),
            "ffprobe": str(tool),
        }
    )

    wait_for_state(manager, record.job_id, "failed")
    current = manager.get(record.job_id)
    assert current.root.is_dir()
    assert current.result_output is None
    assert "destination unavailable" in (current.error or "")
    assert manager.events(record.job_id)[-1]["type"] == "job_failed"
    manager.shutdown()


def test_source_job_rejects_worker_count_above_web_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.wav").write_bytes(b"audio")
    (source / "cover.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")

    try:
        manager.submit({"source_dir": str(source), "workers": 11})
    except RequestValidationError as exc:
        assert "1 到 10" in str(exc)
    else:
        raise AssertionError("worker count above 10 should be rejected")
    manager.shutdown()


def test_suggested_output_name_uses_longest_directory_segment() -> None:
    assert _suggested_output_name(Path(r"D:\AAA\ABCABC\B")) == "ABCABC.mp4"


def test_capabilities_are_derived_from_the_job_model() -> None:
    from dataclasses import fields

    from audiovttforge.job import JobSpec

    declared = {item.name: item.default for item in fields(JobSpec)}
    report = capabilities()

    assert report["api_version"] == "v1"
    assert report["defaults"]["workers"] == declared["workers"]
    assert report["defaults"]["font_color"] == declared["font_color"]
    assert report["defaults"]["subtitle"] == declared["subtitle"]
    assert report["limits"]["workers"] == [1, 10]
    assert report["options"]["workers"] == list(range(1, 11))
    # The browser builds its selects from these lists and then selects the
    # declared default, so every default must be selectable.
    for key, values in (
        ("subtitle", report["subtitle_modes"]),
        ("fps", report["options"]["fps"]),
        ("width", report["options"]["width"]),
        ("workers", report["options"]["workers"]),
        ("font_size", report["options"]["font_size"]),
        ("font_name", report["options"]["font_name"]),
    ):
        assert report["defaults"][key] in values, f"{key} default is not selectable"


def test_progress_events_are_bounded_while_the_total_count_is_kept(tmp_path: Path) -> None:
    tool = tmp_path / "noisy_tool.py"
    tool.write_text(NOISY_TOOL, encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.wav").write_bytes(b"audio")
    (source / "cover.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")

    record = manager.submit(
        {
            "source_dir": str(source),
            "workers": 1,
            "ffmpeg": str(tool),
            "ffprobe": str(tool),
        }
    )
    wait_for_state(manager, record.job_id, "succeeded")

    current = manager.get(record.job_id)
    events = manager.events(record.job_id)
    progress = [event for event in events if event["type"] == "task_progress"]

    assert len(progress) == MAX_RETAINED_PROGRESS_EVENTS
    assert current.event_count > MAX_RETAINED_PROGRESS_EVENTS
    assert current.to_dict()["event_count"] == current.event_count
    sequences = [event["seq"] for event in events]
    assert sequences == sorted(sequences)
    assert events[-1]["type"] == "job_finished"
    assert manager.events(record.job_id, sequences[-2])[-1]["type"] == "job_finished"
    manager.shutdown()


def test_pre_render_failure_is_recorded_as_an_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.wav").write_bytes(b"audio")
    (source / "cover.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")

    def explode(*_args: object, **_kwargs: object) -> None:
        raise ValidationError("cache directory is not writable")

    monkeypatch.setattr("audiovttforge.service.RenderEngine.run", explode)
    record = manager.submit(
        {
            "source_dir": str(source),
            "workers": 1,
            "ffmpeg": str(tool),
            "ffprobe": str(tool),
        }
    )
    wait_for_state(manager, record.job_id, "failed")

    events = manager.events(record.job_id)
    assert [event["type"] for event in events] == ["job_queued", "job_failed"]
    assert "cache directory is not writable" in events[-1]["error"]
    manager.shutdown()


def test_engine_failure_is_not_recorded_twice(tmp_path: Path) -> None:
    tool = tmp_path / "failing_tool.py"
    tool.write_text(FAILING_TOOL, encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.wav").write_bytes(b"audio")
    (source / "cover.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")

    record = manager.submit(
        {
            "source_dir": str(source),
            "workers": 1,
            "ffmpeg": str(tool),
            "ffprobe": str(tool),
        }
    )
    wait_for_state(manager, record.job_id, "failed")

    types = [event["type"] for event in manager.events(record.job_id)]
    assert types.count("job_failed") == 1
    assert types[-1] == "job_failed"
    manager.shutdown()


def test_job_manager_reclaims_stale_caches_on_start(tmp_path: Path) -> None:
    data = tmp_path / "data"
    orphan = data / "jobs" / "deadbeef" / "render"
    orphan.mkdir(parents=True)
    (orphan / "0000.mp4").write_bytes(b"stale render cache")
    expired_upload = data / "uploads" / "expiredupload"
    expired_upload.mkdir(parents=True)
    (expired_upload / "01.wav").write_bytes(b"audio")
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(expired_upload, (old, old))

    manager = JobManager(data)

    assert not (data / "jobs" / "deadbeef").exists()
    assert not expired_upload.exists()
    manager.shutdown()


def test_job_manager_keeps_recent_uploads(tmp_path: Path) -> None:
    data = tmp_path / "data"
    manager = JobManager(data)
    record = manager.upload("01.wav", b"audio")

    assert manager.uploads.prune(60 * 60) == []
    assert manager.uploads.get(record.upload_id).path.is_file()
    assert manager.uploads.prune(0) == [record.upload_id]
    assert not (data / "uploads" / record.upload_id).exists()
    manager.shutdown()


def test_cancel_removes_a_job_that_never_started(tmp_path: Path) -> None:
    """Jobs run one at a time, so a queued job must be cancellable before it runs."""
    tool = tmp_path / "slow_tool.py"
    tool.write_text(SLOW_TOOL, encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.wav").write_bytes(b"audio")
    (source / "cover.png").write_bytes(b"image")
    manager = JobManager(tmp_path / "data")

    def submit() -> object:
        return manager.submit(
            {
                "source_dir": str(source),
                "workers": 1,
                "ffmpeg": str(tool),
                "ffprobe": str(tool),
            }
        )

    running = submit()
    queued = submit()
    try:
        work = running.job.output.parent / f".{running.job.output.stem}_parallel_work"
        started = work / "0000.started"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not started.exists():
            time.sleep(0.02)
        assert started.is_file(), "the first job never started"
        assert manager.get(queued.job_id).state == "queued"
        assert queued.root.is_dir()

        cancelled = manager.cancel(queued.job_id)

        assert cancelled.state == "cancelled"
        assert cancelled.to_dict()["cancelable"] is False
        assert not queued.root.exists()
        assert [event["type"] for event in manager.events(queued.job_id)] == [
            "job_queued",
            "job_cancelled",
        ]
        # Cancelling the queued job must not disturb the running one.
        assert manager.get(running.job_id).state == "running"
    finally:
        manager.cancel(running.job_id)
        manager.shutdown()


def test_source_scan_matches_lrc_for_mp3(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "01.mp3").write_bytes(b"audio")
    (source / "01.mp3.lrc").write_text("[00:01.00]line", encoding="utf-8")
    (source / "cover.png").write_bytes(b"image")

    scan = scan_source_directory(source)

    payload = scan.to_dict()
    assert payload["audio"][0]["subtitle"] == "01.mp3.lrc"
    assert [item["name"] for item in payload["subtitles"]] == ["01.mp3.lrc"]
    assert not any(warning.startswith("缺少字幕") for warning in payload["warnings"])


def test_cancel_stops_ffmpeg_cleans_job_files_and_keeps_source(tmp_path: Path) -> None:
    tool = tmp_path / "slow_tool.py"
    tool.write_text(SLOW_TOOL, encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    audio = source / "01.wav"
    image = source / "cover.png"
    audio.write_bytes(b"audio")
    image.write_bytes(b"image")
    manager = JobManager(tmp_path / "data")

    try:
        record = manager.submit(
            {
                "source_dir": str(source),
                "workers": 1,
                "ffmpeg": str(tool),
                "ffprobe": str(tool),
            }
        )
        work = record.job.output.parent / f".{record.job.output.stem}_parallel_work"
        started = work / "0000.started"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not started.exists():
            time.sleep(0.02)
        assert started.is_file(), "fake FFmpeg did not start"
        process = next(iter(record.active_processes))

        cancelled = manager.cancel(record.job_id)

        assert cancelled.state == "cancelled"
        assert process.poll() is not None
        assert not record.root.exists()
        assert audio.is_file()
        assert image.is_file()
        assert manager.events(record.job_id)[-1]["type"] == "job_cancelled"
        assert cancelled.to_dict()["cancelable"] is False
    finally:
        manager.shutdown()

import time
from pathlib import Path

from audiovttforge.service import (
    JobManager,
    NotFoundError,
    RequestValidationError,
    _suggested_output_name,
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
    assert current.result_output is not None and current.result_output.is_file()
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

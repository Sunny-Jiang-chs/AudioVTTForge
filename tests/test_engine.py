import json
import sys
from pathlib import Path

import pytest

from audiovttforge.engine import EngineError, RenderEngine, validate_job
from audiovttforge.job import JobSpec


FAKE_TOOL = """\
import json
import pathlib
import sys

args = sys.argv[1:]
if "-version" in args:
    print("fake-media-tool 1.0")
    raise SystemExit(0)
if "-show_entries" in args:
    print(json.dumps({"streams": [{"duration": "1.250"}]}))
    raise SystemExit(0)
if "--fail" in args:
    print("forced failure")
    raise SystemExit(2)
output = pathlib.Path(args[-1])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b"fake mp4")
print("frame=1 time=00:00:01.250")
"""


def make_job(tmp_path: Path, tool: Path) -> JobSpec:
    audio = tmp_path / "01.wav"
    image = tmp_path / "cover.png"
    vtt = tmp_path / "01.wav.vtt"
    audio.write_bytes(b"audio")
    image.write_bytes(b"image")
    vtt.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n", encoding="utf-8")
    return JobSpec(
        audio=(audio,),
        images=(image,),
        assignments=(0,),
        output=tmp_path / "result.mp4",
        workers=1,
        ffmpeg=tool,
        ffprobe=tool,
    )


def test_engine_runs_without_gui(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    job = make_job(tmp_path, tool)
    events: list[dict] = []

    result = RenderEngine(events.append).run(job, keep_work=True)

    assert result.output.is_file()
    assert result.work.is_dir()
    assert result.summary.is_file()
    event_types = [event["type"] for event in events]
    assert event_types[0] == "job_started"
    assert "task_finished" in event_types
    assert event_types[-1] == "job_finished"
    logged_types = [
        json.loads(line)["type"]
        for line in result.events.read_text(encoding="utf-8").splitlines()
    ]
    assert logged_types[-1] == "job_finished"


def test_engine_resume_skips_existing_segment(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    job = make_job(tmp_path, tool)
    RenderEngine().run(job, keep_work=True)
    events: list[dict] = []

    RenderEngine(events.append).run(job, keep_work=True, resume=True)

    assert "task_skipped" in [event["type"] for event in events]


def test_failed_run_keeps_diagnostics_and_failure_event(tmp_path: Path) -> None:
    probe_tool = tmp_path / "probe_tool.py"
    probe_tool.write_text(FAKE_TOOL, encoding="utf-8")
    failing_tool = tmp_path / "failing_tool.py"
    failing_tool.write_text(FAKE_TOOL.replace("if \"--fail\" in args:", "if True:"), encoding="utf-8")
    job = make_job(tmp_path, failing_tool)
    job = JobSpec(
        **{
            **job.__dict__,
            "ffprobe": probe_tool,
        }
    )

    with pytest.raises(EngineError):
        RenderEngine().run(job)

    events = [
        json.loads(line)
        for line in job.output.parent.joinpath(".result.events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["type"] == "job_started"
    assert events[-1]["type"] == "job_failed"
    summary = json.loads(job.output.parent.joinpath(".result.summary.json").read_text(encoding="utf-8"))
    assert summary["success"] is False


def test_validation_reports_missing_inputs(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    job = JobSpec(
        audio=(tmp_path / "missing.wav",),
        images=(tmp_path / "missing.png",),
        assignments=(0,),
        output=tmp_path / "result.mp4",
        ffmpeg=tool,
        ffprobe=tool,
    )

    errors = validate_job(job)

    assert any("Missing audio" in error for error in errors)
    assert any("Missing image" in error for error in errors)
    assert any("Missing VTT" in error for error in errors)

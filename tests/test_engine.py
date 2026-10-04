import json
import os
import sys
from pathlib import Path

import pytest

from audiovttforge.engine import EngineError, RenderEngine, _tool_environment, validate_job
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


def test_embedded_subtitle_input_precedes_output_options(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    job = make_job(tmp_path, tool)
    job = JobSpec(
        **{
            **job.__dict__,
            "subtitle": "embedded",
        }
    )

    command = RenderEngine()._render_command(
        job,
        index=0,
        duration=1.25,
        segment=tmp_path / "segment.mp4",
        subtitle_path=None,
    )

    vtt_index = command.index(str(job.audio[0].with_name(job.audio[0].name + ".vtt")))
    assert command[vtt_index - 1] == "-i"
    assert command.index("-map") < command.index("-vf")
    assert command.index("2:0") < command.index("-vf")
    assert command.index("-c:s") < command.index("-movflags")


def test_merge_preserves_all_streams() -> None:
    command = RenderEngine()._merge_command(
        Path("ffmpeg.exe"),
        Path("segments.txt"),
        Path("merged.mp4"),
    )

    assert command[command.index("-map") + 1] == "0"
    assert command.index("-map") < command.index("-c")


def test_tool_environment_prepends_neighbor_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = tmp_path / "bin" / "ffmpeg.exe"
    tool.parent.mkdir()
    monkeypatch.setenv("PATH", "existing-entry")

    environment = _tool_environment(tool)

    entries = environment["PATH"].split(os.pathsep)
    assert entries[0] == str(tool.parent.resolve())
    assert entries[1:] == ["existing-entry"]


def test_merge_existing_uses_numbered_segments_in_order(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    work = tmp_path / ".result_parallel_work"
    work.mkdir()
    (work / "0001.mp4").write_bytes(b"second")
    (work / "0000.mp4").write_bytes(b"first")
    (work / "notes.mp4").write_bytes(b"ignored")

    output = tmp_path / "recovered.mp4"
    result = RenderEngine().merge_existing(work, output, tool)

    assert result == output
    assert output.read_bytes() == b"fake mp4"
    concat = (work / "segments.txt").read_text(encoding="utf-8")
    assert concat.splitlines() == [
        f"file '{(work / '0000.mp4').resolve().as_posix()}'",
        f"file '{(work / '0001.mp4').resolve().as_posix()}'",
    ]
    events = [
        json.loads(line)["type"]
        for line in (work / "merge_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["merge_started", "merge_finished"]


def test_embedded_subtitle_runs_with_fake_tools(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    job = make_job(tmp_path, tool)
    job = JobSpec(**{**job.__dict__, "subtitle": "embedded"})

    result = RenderEngine().run(job, keep_work=True)

    assert result.output.is_file()


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
    assert not any("Missing VTT" in error for error in errors)


def test_engine_allows_missing_vtt_and_renders_without_subtitles(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    job = make_job(tmp_path, tool)
    job.audio[0].with_name(job.audio[0].name + ".vtt").unlink()
    events: list[dict] = []

    result = RenderEngine(events.append).run(job, keep_work=True)

    assert result.output.is_file()
    assert "subtitle_missing" in [event["type"] for event in events]
    assert not (result.work / "0000.srt").exists()

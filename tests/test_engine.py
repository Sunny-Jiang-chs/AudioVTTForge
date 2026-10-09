import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from audiovttforge.engine import (
    EngineError,
    RenderEngine,
    _tool_environment,
    concat_line,
    escape_filter_path,
    plan_job,
    validate_job,
)
from audiovttforge.job import DEFAULT_FFMPEG, JobSpec


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

LOUD_FAILURE_TOOL = """\
import json
import sys

args = sys.argv[1:]
if "-show_entries" in args:
    print(json.dumps({"streams": [{"duration": "1.250"}]}))
    raise SystemExit(0)
print("MARKER-no-option-name-near-Microsoft-YaHei")
print("MARKER-second-line")
raise SystemExit(3)
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
    task_started = next(event for event in events if event["type"] == "task_started")
    video_filter = task_started["command"][task_started["command"].index("-vf") + 1]
    assert "subtitles=" in video_filter
    assert "0000.srt" in video_filter
    assert "FontName=Microsoft YaHei" in video_filter
    assert "FontSize=42" in video_filter
    assert "PrimaryColour=&H00FFFFFF" in video_filter
    assert plan_job(job)["tasks"][0]["subtitle"].endswith("01.wav.vtt")
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

    subtitle = job.audio[0].with_name(job.audio[0].name + ".vtt")
    command = RenderEngine()._render_command(
        job,
        index=0,
        duration=1.25,
        segment=tmp_path / "segment.mp4",
        subtitle_path=subtitle,
    )

    vtt_index = command.index(str(subtitle))
    assert command[vtt_index - 1] == "-i"
    assert command.index("-map") < command.index("-vf")
    assert command.index("2:0") < command.index("-vf")
    assert command.index("-c:s") < command.index("-movflags")


def test_render_command_uses_no_subtitle_when_none_is_resolved(tmp_path: Path) -> None:
    """The caller decides; an unresolved subtitle must not be re-discovered."""
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    job = make_job(tmp_path, tool)
    job = JobSpec(**{**job.__dict__, "subtitle": "embedded"})

    command = RenderEngine()._render_command(
        job,
        index=0,
        duration=1.25,
        segment=tmp_path / "segment.mp4",
        subtitle_path=None,
    )

    assert str(job.audio[0].with_name(job.audio[0].name + ".vtt")) not in command
    assert "2:0" not in command
    assert "-c:s" not in command


def test_engine_converts_lrc_for_burnin_and_embedded_modes(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    audio = tmp_path / "01.mp3"
    audio.write_bytes(b"audio")
    (tmp_path / "01.lrc").write_text("[00:00.00]hello\n[00:01.00]world", encoding="utf-8")
    image = tmp_path / "cover.png"
    image.write_bytes(b"image")
    base_job = JobSpec(
        audio=(audio,), images=(image,), assignments=(0,), output=tmp_path / "burned.mp4",
        workers=1, ffmpeg=tool, ffprobe=tool,
    )

    burned = RenderEngine().run(base_job, keep_work=True)
    converted = (burned.work / "0000.srt").read_text(encoding="utf-8")
    assert "hello" in converted and "world" in converted
    burn_command = next(
        json.loads(line)["command"]
        for line in burned.events.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["type"] == "task_started"
    )
    assert "subtitles=" in burn_command[burn_command.index("-vf") + 1]

    embedded_job = JobSpec(**{**base_job.__dict__, "output": tmp_path / "embedded.mp4", "subtitle": "embedded"})
    embedded = RenderEngine().run(embedded_job, keep_work=True)
    embedded_command = next(
        json.loads(line)["command"]
        for line in embedded.events.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["type"] == "task_started"
    )
    assert str(embedded.work / "0000.srt") in embedded_command
    mapped_streams = [
        embedded_command[index + 1]
        for index, value in enumerate(embedded_command[:-1])
        if value == "-map"
    ]
    assert "2:0" in mapped_streams


def test_burnin_subtitle_style_uses_job_settings(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    base_job = make_job(tmp_path, tool)
    job = JobSpec(**{
        **base_job.__dict__,
        "font_name": "Arial",
        "font_size": 56,
        "font_color": "#12ABEF",
    })
    subtitle_path = tmp_path / "01.wav.srt"
    subtitle_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")

    command = RenderEngine()._render_command(
        job,
        index=0,
        duration=1.25,
        segment=tmp_path / "segment.mp4",
        subtitle_path=subtitle_path,
    )

    video_filter = command[command.index("-vf") + 1]
    assert "FontName=Arial" in video_filter
    assert "FontSize=56" in video_filter
    assert "PrimaryColour=&H00EFAB12" in video_filter


def test_validate_job_rejects_unsafe_subtitle_style(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    base_job = make_job(tmp_path, tool)
    job = JobSpec(**{
        **base_job.__dict__,
        "font_name": "Arial,Injected",
        "font_size": 200,
        "font_color": "red",
    })

    errors = validate_job(job, check_tools=False)

    assert any("Font name" in error for error in errors)
    assert any("Font size" in error for error in errors)
    assert any("Font color" in error for error in errors)


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
    task_started = next(event for event in events if event["type"] == "task_started")
    video_filter = task_started["command"][task_started["command"].index("-vf") + 1]
    assert "subtitles=" not in video_filter


def test_engine_skips_a_subtitle_file_without_cues(tmp_path: Path) -> None:
    """A placeholder subtitle must not fail the render (libass cannot open 0 bytes)."""
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    job = make_job(tmp_path, tool)
    job.audio[0].with_name(job.audio[0].name + ".vtt").write_text("WEBVTT\n", encoding="utf-8")
    events: list[dict] = []

    result = RenderEngine(events.append).run(job, keep_work=True)

    assert result.output.is_file()
    assert "subtitle_empty" in [event["type"] for event in events]
    assert not (result.work / "0000.srt").exists()
    task_started = next(event for event in events if event["type"] == "task_started")
    assert "subtitles=" not in task_started["command"][task_started["command"].index("-vf") + 1]


def test_engine_reports_which_subtitle_could_not_be_parsed(tmp_path: Path) -> None:
    tool = tmp_path / "fake_tool.py"
    tool.write_text(FAKE_TOOL, encoding="utf-8")
    job = make_job(tmp_path, tool)
    broken = job.audio[0].with_name(job.audio[0].name + ".vtt")
    broken.write_text("WEBVTT\n\n00:00.000 --> not-a-time\nhello\n", encoding="utf-8")

    with pytest.raises(EngineError) as failure:
        RenderEngine().run(job)

    message = str(failure.value)
    assert "Subtitle could not be parsed" in message
    assert str(broken) in message


def test_render_failure_includes_the_ffmpeg_log_tail(tmp_path: Path) -> None:
    """The user must not have to hunt a temp log that is purged on restart."""
    tool = tmp_path / "loud_tool.py"
    tool.write_text(LOUD_FAILURE_TOOL, encoding="utf-8")
    job = make_job(tmp_path, tool)

    with pytest.raises(EngineError) as failure:
        RenderEngine().run(job)

    message = str(failure.value)
    assert "MARKER-no-option-name-near-Microsoft-YaHei" in message
    assert "MARKER-second-line" in message


def test_escape_filter_path_escapes_graph_and_option_specials() -> None:
    # ":" is escaped for the option parser only (two backslashes); the graph
    # parser escapes ' [ ] , ; as well, so those carry three.
    assert escape_filter_path(Path(r"D:\a b\Bob's [final], v2; work\0000.srt")) == (
        r"D\\:/a b/Bob\\\'s \\\[final\\\]\\\, v2\\\; work/0000.srt"
    )
    assert escape_filter_path(Path("D:/plain/0000.srt")) == r"D\\:/plain/0000.srt"


def test_concat_line_survives_an_apostrophe_in_the_path(tmp_path: Path) -> None:
    segment = tmp_path / ".Bob's [final], v2; work" / "0000.mp4"

    line = concat_line(segment)

    # file '<part>'\''<rest>' -- two delimiters plus the three-character escape
    assert line.startswith("file '") and line.endswith("'")
    assert "'\\''" in line
    assert line.count("'") == 5


@pytest.mark.skipif(not DEFAULT_FFMPEG.is_file(), reason="real FFmpeg is not installed")
def test_real_ffmpeg_burns_subtitles_from_a_path_with_filter_specials(tmp_path: Path) -> None:
    """The escaping fix verified against a real FFmpeg, not a fake tool."""
    source = tmp_path / ".Bob's [final], v2; work"
    source.mkdir()
    audio = source / "01.wav"
    image = source / "cover.png"
    vtt = source / "01.wav.vtt"
    subprocess.run(
        [str(DEFAULT_FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "48000", "-ac", "2", str(audio)],
        check=True,
    )
    subprocess.run(
        [str(DEFAULT_FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=navy:s=320x180:d=1", "-frames:v", "1", str(image)],
        check=True,
    )
    vtt.write_text("WEBVTT\n\n00:00.000 --> 00:00.900\nhello\n", encoding="utf-8")

    job = JobSpec(
        audio=(audio,),
        images=(image,),
        assignments=(0,),
        output=source / "out.mp4",
        subtitle="burnin",
        fps=2,
        width=320,
        workers=1,
    )
    RenderEngine().run(job, keep_work=True)

    assert job.output.is_file()
    frame = source / "frame.png"
    subprocess.run(
        [str(DEFAULT_FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(job.output), "-vf", "select=eq(n\\,1)", "-frames:v", "1", str(frame)],
        check=True,
    )
    # Burn-in must have changed pixels; a plain navy frame would be tiny.
    assert frame.stat().st_size > 3000

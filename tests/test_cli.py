import json
from pathlib import Path

from audiovttforge.cli import main
from audiovttforge.job import load_job


def test_plan_cli_uses_job_file_relative_paths(tmp_path: Path, capsys) -> None:
    audio = tmp_path / "01.wav"
    image = tmp_path / "cover.png"
    audio.write_bytes(b"audio")
    image.write_bytes(b"image")
    (tmp_path / "01.wav.vtt").write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n",
        encoding="utf-8",
    )
    job_path = tmp_path / "job.json"
    job_path.write_text(
        json.dumps(
            {
                "audio": ["01.wav"],
                "images": ["cover.png"],
                "output": "result.mp4",
                "subtitle": "none",
                "workers": 1,
            }
        ),
        encoding="utf-8",
    )

    assert main(["plan", str(job_path)]) == 0
    plan = json.loads(capsys.readouterr().out)

    assert plan["valid"] is True
    assert plan["tasks"][0]["audio"] == str(audio)
    assert plan["tasks"][0]["image"] == str(image)


def test_job_tools_are_relative_to_job_file(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    ffmpeg = tools / "ffmpeg.exe"
    ffprobe = tools / "ffprobe.exe"
    ffmpeg.write_bytes(b"fake")
    ffprobe.write_bytes(b"fake")
    job_path = tmp_path / "job.json"
    job_path.write_text(
        json.dumps(
            {
                "audio": [],
                "images": [],
                "output": "result.mp4",
                "ffmpeg": "tools/ffmpeg.exe",
                "ffprobe": "tools/ffprobe.exe",
            }
        ),
        encoding="utf-8",
    )

    job = load_job(job_path)

    assert job.ffmpeg == ffmpeg
    assert job.ffprobe == ffprobe

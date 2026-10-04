from pathlib import Path

import pytest

from audiovttforge.media import (
    automatic_image_index,
    parse_time,
    read_vtt,
    srt_time,
    video_dimensions,
)


def test_parse_and_format_time() -> None:
    assert parse_time("00:01.250") == 1250
    assert parse_time("01:02:03,004") == 3_723_004
    assert srt_time(3_723_004) == "01:02:03,004"


def test_read_vtt_strips_markup(tmp_path: Path) -> None:
    path = tmp_path / "sample.vtt"
    path.write_text(
        "WEBVTT\n\n00:00.000 --> 00:01.500\n<c.green>Hello<br>world</c>\n",
        encoding="utf-8",
    )

    assert read_vtt(path)[0].text == "Hello\nworld"


def test_assignment_and_dimensions() -> None:
    assert [automatic_image_index(i, 5, 2) for i in range(5)] == [0, 0, 0, 1, 1]
    assert video_dimensions(1920) == (1920, 1080)
    with pytest.raises(ValueError):
        video_dimensions(1919)

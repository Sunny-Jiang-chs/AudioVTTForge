from pathlib import Path

import pytest

from audiovttforge.media import (
    automatic_image_index,
    find_subtitle,
    merge_subtitle_cues,
    parse_time,
    read_lrc,
    read_vtt,
    srt_time,
    video_dimensions,
    write_vtt,
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


def test_read_lrc_supports_offsets_multiple_timestamps_and_milliseconds(tmp_path: Path) -> None:
    path = tmp_path / "sample.lrc"
    path.write_text(
        "[ar:Artist]\n[offset:-500]\n[00:01.2][00:03.45]First line\n[00:05.678]Second line\n",
        encoding="utf-8",
    )

    cues = read_lrc(path, end_time_ms=7000)

    assert [(cue.start, cue.end, cue.text) for cue in cues] == [
        (700, 2950, "First line"),
        (2950, 5178, "First line"),
        (5178, 7000, "Second line"),
    ]


def test_merge_subtitle_cues_offsets_vtt_and_lrc_across_missing_audio(tmp_path: Path) -> None:
    first = tmp_path / "01.wav"
    missing = tmp_path / "02.wav"
    last = tmp_path / "03.wav"
    for path in (first, missing, last):
        path.write_bytes(b"audio")
    first.with_name("01.wav.vtt").write_text(
        "WEBVTT\n\n00:00.500 --> 00:03.000\nFirst\n",
        encoding="utf-8",
    )
    last.with_name("03.wav.lrc").write_text("[00:00.25]Last\n", encoding="utf-8")

    cues = merge_subtitle_cues((first, missing, last), (2000, 1000, 2000))
    output = tmp_path / "merged.vtt"
    write_vtt(cues, output)

    assert [(cue.start, cue.end, cue.text) for cue in cues] == [
        (500, 2000, "First"),
        (3250, 5000, "Last"),
    ]
    assert output.read_text(encoding="utf-8") == (
        "WEBVTT\n\n"
        "00:00:00.500 --> 00:00:02.000\nFirst\n\n"
        "00:00:03.250 --> 00:00:05.000\nLast\n"
    )


def test_find_subtitle_supports_audio_and_stem_suffixes_with_vtt_precedence(tmp_path: Path) -> None:
    audio = tmp_path / "01.mp3"
    audio.write_bytes(b"audio")
    stem_lrc = tmp_path / "01.lrc"
    stem_lrc.write_text("[00:01.00]line", encoding="utf-8")

    assert find_subtitle(audio) == stem_lrc
    audio_lrc = tmp_path / "01.mp3.lrc"
    audio_lrc.write_text("[00:01.00]line", encoding="utf-8")
    assert find_subtitle(audio) == audio_lrc
    vtt = tmp_path / "01.mp3.vtt"
    vtt.write_text("WEBVTT", encoding="utf-8")
    assert find_subtitle(audio) == vtt


def test_assignment_and_dimensions() -> None:
    assert [automatic_image_index(i, 5, 2) for i in range(5)] == [0, 0, 0, 1, 1]
    assert video_dimensions(1920) == (1920, 1080)
    with pytest.raises(ValueError):
        video_dimensions(1919)

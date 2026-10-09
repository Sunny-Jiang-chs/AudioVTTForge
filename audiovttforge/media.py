"""Pure media and subtitle helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTENSIONS = {".wav", ".mp3"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
SUBTITLE_EXTENSIONS = {".vtt", ".lrc"}
_LRC_TIMESTAMP = re.compile(r"\[(\d+):(\d{2})(?::(\d{2}))?(?:\.(\d{1,3}))?\]")


@dataclass(frozen=True)
class Cue:
    start: int
    end: int
    text: str


def parse_time(value: str) -> int:
    match = re.match(r"^(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid timestamp: {value}")
    return (
        (int(match.group(1) or 0) * 60 + int(match.group(2))) * 60
        + int(match.group(3))
    ) * 1000 + int(match.group(4))


def srt_time(value: int) -> str:
    hours, rest = divmod(max(0, value), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def read_vtt(path: Path) -> list[Cue]:
    lines = (
        path.read_text(encoding="utf-8-sig")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .split("\n")
    )
    cues: list[Cue] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.upper() in {"WEBVTT", "NOTE", "STYLE", "REGION"}:
            index += 1
            continue
        if "-->" not in line:
            index += 1
            continue
        left, right = line.split("-->", 1)
        start, end = parse_time(left), parse_time(right.split()[0])
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index])
            index += 1
        text = re.sub(r"(?i)<br\s*/?>", "\n", "\n".join(text_lines))
        text = re.sub(
            r"(?i)</?(?:c|v|lang|i|b|u|ruby|rt)(?:\.[^ >]+)?(?:\s+[^>]*)?>",
            "",
            text,
        )
        text = re.sub(r"<[^>]+>", "", text).strip()
        if text and end > start:
            cues.append(Cue(start, end, text))
    return cues


def subtitle_candidates(audio: Path) -> tuple[Path, ...]:
    return tuple(
        audio.with_name(name)
        for name in (
            f"{audio.name}.vtt",
            f"{audio.name}.lrc",
            f"{audio.stem}.vtt",
            f"{audio.stem}.lrc",
        )
    )


def find_subtitle(audio: Path) -> Path | None:
    return next((path for path in subtitle_candidates(audio) if path.is_file()), None)


def read_lrc(path: Path, end_time_ms: int | None = None) -> list[Cue]:
    lines = path.read_text(encoding="utf-8-sig").replace("\r", "").split("\n")
    offset_ms = 0
    for line in lines:
        offset = re.fullmatch(r"\s*\[offset\s*:\s*([+-]?\d+)\s*\]\s*", line, re.IGNORECASE)
        if offset:
            offset_ms = int(offset.group(1))

    timed_lines: list[tuple[int, str]] = []
    for line in lines:
        remaining = line.strip()
        timestamps: list[int] = []
        while match := _LRC_TIMESTAMP.match(remaining):
            first, second, third, fraction = match.groups()
            if third is None:
                minutes, seconds = int(first), int(second)
                hours = 0
            else:
                hours, minutes, seconds = int(first), int(second), int(third)
            if seconds >= 60 or minutes >= 60 and third is not None:
                break
            milliseconds = int((fraction or "0").ljust(3, "0"))
            timestamp = ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds
            timestamps.append(max(0, timestamp + offset_ms))
            remaining = remaining[match.end():].lstrip()
        text = re.sub(r"<\d+:\d{2}(?:\.\d{1,3})?>", "", remaining).strip()
        if timestamps and text:
            timed_lines.extend((timestamp, text) for timestamp in timestamps)

    timed_lines.sort(key=lambda item: item[0])
    starts = sorted({timestamp for timestamp, _ in timed_lines})
    end_by_start = {
        start: starts[index + 1] if index + 1 < len(starts) else (end_time_ms or start + 3000)
        for index, start in enumerate(starts)
    }
    cues: list[Cue] = []
    for start, text in timed_lines:
        cues.append(Cue(start, max(start + 1, end_by_start[start]), text))
    return cues


def read_subtitle(path: Path, end_time_ms: int | None = None) -> list[Cue]:
    if path.suffix.lower() == ".lrc":
        return read_lrc(path, end_time_ms)
    return read_vtt(path)


def write_srt(cues: list[Cue], path: Path) -> None:
    lines: list[str] = []
    for number, cue in enumerate(cues, 1):
        lines += [
            str(number),
            f"{srt_time(cue.start)} --> {srt_time(cue.end)}",
            cue.text,
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def automatic_image_index(audio_index: int, audio_count: int, image_count: int) -> int:
    if audio_count < 1 or image_count < 1:
        raise ValueError("audio_count and image_count must be positive")
    if not 0 <= audio_index < audio_count:
        raise ValueError("audio_index is outside the audio range")
    return min(image_count - 1, audio_index * image_count // audio_count)


def video_dimensions(width: int) -> tuple[int, int]:
    if width < 2 or width % 2:
        raise ValueError("video width must be a positive even number")
    height = round(width * 9 / 16)
    return width, height + height % 2

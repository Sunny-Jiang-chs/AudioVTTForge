"""Pure media and subtitle helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

AUDIO_EXTENSIONS = {".wav", ".mp3"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


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

"""Command-line interface for the headless AudioVTTForge engine."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .engine import (
    RenderEngine,
    inspect_tool,
    plan_job,
    validate_job,
)
from .job import default_ffmpeg, default_ffprobe, load_job, resolve_tool


def _json_print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audiovttforge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a job JSON file")
    validate.add_argument("job", type=Path)

    plan = subparsers.add_parser("plan", help="show the planned work without running it")
    plan.add_argument("job", type=Path)

    run = subparsers.add_parser("run", help="run a job and emit JSONL events")
    run.add_argument("job", type=Path)
    run.add_argument("--keep-work", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--workers", type=int)
    run.add_argument("--ffmpeg")
    run.add_argument("--ffprobe")

    merge = subparsers.add_parser("merge", help="merge existing numbered MP4 segments")
    merge.add_argument("work", type=Path)
    merge.add_argument("output", type=Path)
    merge.add_argument("--ffmpeg")

    doctor = subparsers.add_parser("doctor", help="inspect local media tool availability")
    doctor.add_argument("--ffmpeg")
    doctor.add_argument("--ffprobe")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"validate", "plan", "run"}:
            job = load_job(args.job)
        if args.command == "validate":
            errors = validate_job(job)
            _json_print({"valid": not errors, "errors": errors})
            return 0 if not errors else 1
        if args.command == "plan":
            plan = plan_job(job)
            _json_print(plan)
            return 0 if not plan["errors"] else 1
        if args.command == "run":
            overrides = {}
            if args.workers is not None:
                overrides["workers"] = args.workers
            if args.ffmpeg:
                overrides["ffmpeg"] = resolve_tool(args.ffmpeg, "", default_ffmpeg(), "ffmpeg")
            if args.ffprobe:
                overrides["ffprobe"] = resolve_tool(args.ffprobe, "", default_ffprobe(), "ffprobe")
            if overrides:
                job = replace(job, **overrides)
            callback = lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)
            result = RenderEngine(callback).run(
                job,
                keep_work=args.keep_work,
                resume=args.resume,
            )
            print(json.dumps({"type": "result", "output": str(result.output)}, ensure_ascii=False))
            return 0
        if args.command == "merge":
            ffmpeg = resolve_tool(args.ffmpeg, "", default_ffmpeg(), "ffmpeg")
            output = RenderEngine().merge_existing(args.work, args.output, ffmpeg)
            _json_print({"merged": str(output)})
            return 0
        if args.command == "doctor":
            ffmpeg = resolve_tool(args.ffmpeg, "", default_ffmpeg(), "ffmpeg")
            ffprobe = resolve_tool(args.ffprobe, "", default_ffprobe(), "ffprobe")
            report = {"ffmpeg": inspect_tool(ffmpeg), "ffprobe": inspect_tool(ffprobe)}
            _json_print(report)
            return 0 if report["ffmpeg"]["available"] and report["ffprobe"]["available"] else 1
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 1

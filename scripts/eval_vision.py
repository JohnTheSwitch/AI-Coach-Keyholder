"""
scripts/eval_vision.py

Offline evaluation of the local vision model (v0.39 Phase H1) on YOUR sample photos, before anything is connected to Chaster.

    python -m scripts.eval_vision --type device_check --code ABC123 --expect verified photos\\locked_1.jpg photos\\locked_2.jpg
    python -m scripts.eval_vision --type device_check --expect not_verified photos\\not_worn\\
    python -m scripts.eval_vision --type task_proof --task "Make the bed" --code XYZ9 --repeat 3 photos\\bed.jpg

Each photo is read into memory (at most 8 MB), checked to be a JPEG, PNG or WebP, sent to the LOCAL Ollama (a non-local OLLAMA_HOST is refused) and its
buffers are overwritten afterwards. The script prints one line per photo and a summary and writes nothing anywhere: no copy, no log, no cache. Without
--code the verification-code check is skipped (in the live pipeline it is mandatory); a "verified" answer then stands on the picture alone.

The model is VISION_OLLAMA_MODEL from .env, or --model to try one before enabling it. The first call includes loading the model.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from collections import Counter
from pathlib import Path

from core.config import Config
from vision.adapter import VisionAdapter
from vision.fetch import MAX_IMAGE_BYTES, check_image, wipe
from vision.models import Verdict, VerificationType, VisionError

_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _photos(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found += sorted(x for x in p.iterdir() if x.is_file() and x.suffix.lower() in _SUFFIXES)
        elif p.is_file():
            found.append(p)
        else:
            print(f"  skipped (not found): {raw}")
    return found


def _read(path: Path) -> bytearray | None:
    size = os.stat(path).st_size
    if size == 0 or size > MAX_IMAGE_BYTES:
        print(f"  {path.name}: skipped (size {size} bytes; the limit is {MAX_IMAGE_BYTES})")
        return None
    buffer = bytearray(size)
    with open(path, "rb") as handle:                 # read only; nothing is ever written
        handle.readinto(buffer)
    try:
        check_image(buffer)
    except VisionError:
        wipe(buffer)
        print(f"  {path.name}: skipped (not a JPEG, PNG or WebP)")
        return None
    return buffer


def main(argv: list[str], *, adapter_factory=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.eval_vision", description="Try the local vision model on your own photos (memory only).")
    parser.add_argument("photos", nargs="+", help="image files or folders")
    parser.add_argument("--type", required=True, choices=[t.value for t in VerificationType])
    parser.add_argument("--code", default=None, help="the verification code visible in the photos (enables the code check)")
    parser.add_argument("--task", default=None, help="the task text, for --type task_proof")
    parser.add_argument("--expect", default=None, choices=[v.value for v in Verdict], help="the verdict you expect, to measure agreement")
    parser.add_argument("--repeat", type=int, default=1, help="evaluate each photo this many times (consistency)")
    parser.add_argument("--model", default=None, help="override VISION_OLLAMA_MODEL")
    parser.add_argument("--timeout", type=float, default=None, help="seconds per evaluation (default VISION_READ_TIMEOUT_SECONDS)")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    config = Config.load()
    model = args.model or config.vision_ollama_model
    if not model:
        print("No vision model: set VISION_OLLAMA_MODEL in .env or pass --model (for example --model qwen3-vl:8b-instruct).")
        return 2
    try:
        adapter = (adapter_factory or VisionAdapter)(host=config.ollama_host, model=model,
                                                    read_timeout_seconds=args.timeout or config.vision_read_timeout_seconds,
                                                    keep_alive=config.ollama_keep_alive or "10m")
    except VisionError as exc:
        print(f"Refused: {exc.code} (the vision model must run on this machine).")
        return 2

    photos = _photos(args.photos)
    print(f"Model {model}, check {args.type}, code check {'on' if args.code else 'OFF'}, {len(photos)} photo(s) x {args.repeat}.")
    verdicts: Counter = Counter()
    latencies: list[int] = []
    agree = total = errors = 0
    for path in photos:
        buffer = _read(path)
        if buffer is None:
            continue
        try:
            for round_no in range(args.repeat):
                try:
                    result = adapter.evaluate(buffer, verification_type=VerificationType(args.type), expected_code=args.code, task_text=args.task,
                                              require_code=bool(args.code))
                except VisionError as exc:
                    errors += 1
                    print(f"  {path.name} #{round_no + 1}: ERROR {exc.code} (nothing decided)")
                    continue
                total += 1
                verdicts[result.verdict.value] += 1
                latencies.append(result.latency_ms)
                match = "" if result.code_matched is None else f" code {'ok' if result.code_matched else 'MISMATCH'}"
                mark = ""
                if args.expect:
                    hit = result.verdict.value == args.expect
                    agree += hit
                    mark = "  [as expected]" if hit else f"  [expected {args.expect}]"
                print(f"  {path.name} #{round_no + 1}: {result.verdict.value} ({result.reason_code.value}){match} {result.latency_ms} ms{mark}")
        finally:
            wipe(buffer)

    print("\nSummary:", dict(verdicts) or "no results", f"| errors {errors}")
    if latencies:
        print(f"Latency ms: median {int(statistics.median(latencies))}, max {max(latencies)} (the first call includes loading the model)")
    if args.expect and total:
        print(f"Agreement with --expect {args.expect}: {agree}/{total} ({100 * agree / total:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

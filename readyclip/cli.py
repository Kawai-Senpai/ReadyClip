"""Command-line interface. Unset flags fall back to the saved GUI settings."""

from __future__ import annotations

import argparse
import sys
import threading
from dataclasses import asdict

from readyclip import APP_NAME, __version__
from readyclip.core import (
    CODECS, CUSTOM, FPS_CAPS, JOBS, PRESETS, QUALITIES, RESOLUTIONS, Settings,
    apply_preset, collect_videos, config_path, convert_many, free_vram_mb,
    gpu_available, load_settings, pick_jobs, save_settings,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="readyclip",
        description="Convert action-cam videos into phone-friendly MP4s. "
                    "Output goes into a subfolder next to each source file, "
                    "or into --out-dir.",
    )
    p.add_argument("paths", nargs="*", help="video files and/or folders")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="also look inside subfolders")
    p.add_argument("--preset", choices=list(PRESETS),
                   help="start from a preset; other flags override it. "
                        + ", ".join(f"{k} = {v[0]}" for k, v in PRESETS.items()))
    p.add_argument("--res", type=int, choices=sorted(RESOLUTIONS),
                   help="max short side in pixels, 0 = keep original")
    p.add_argument("--quality", choices=list(QUALITIES))
    p.add_argument("--codec", choices=list(CODECS))
    p.add_argument("--fps", type=int, choices=sorted(FPS_CAPS),
                   help="frame-rate cap, 0 = keep original")
    gpu = p.add_mutually_exclusive_group()
    gpu.add_argument("--gpu", dest="use_gpu", action="store_true", default=None,
                     help="use NVIDIA NVENC when available")
    gpu.add_argument("--cpu", dest="use_gpu", action="store_false",
                     help="never use the GPU")
    p.add_argument("--jobs", type=int, choices=sorted(JOBS),
                   help="files converted at once, 0 = auto from free GPU memory")
    p.add_argument("--max-size", dest="max_size_mb", type=int, metavar="MB",
                   help="keep each output under this many MB, 0 = no limit")
    size = p.add_mutually_exclusive_group()
    size.add_argument("--split", dest="size_mode", action="store_const", const="split",
                      help="over the size limit: split into parts, keep quality")
    size.add_argument("--shrink", dest="size_mode", action="store_const", const="shrink",
                      help="over the size limit: lower quality to fit (default)")
    p.add_argument("--out", dest="subfolder", help="output subfolder name")
    p.add_argument("--out-dir", dest="output_dir", metavar="FOLDER",
                   help='put every output in this one folder ("" = subfolder next to each video)')
    p.add_argument("--overwrite", action="store_true",
                   help="re-convert files that already have an output")
    p.add_argument("--save", action="store_true",
                   help="store these options as the new defaults")
    p.add_argument("--show-config", action="store_true",
                   help="print the current defaults and exit")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    try:
        if args.preset:
            settings = apply_preset(settings, args.preset)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    preset_fields = {"res", "quality", "codec", "fps", "max_size_mb", "size_mode"}
    for name in ("res", "quality", "codec", "fps", "use_gpu", "subfolder", "jobs",
                 "max_size_mb", "size_mode", "output_dir"):
        value = getattr(args, name)
        if value is not None:
            field = {"res": "resolution", "fps": "max_fps"}.get(name, name)
            setattr(settings, field, value)
            if name in preset_fields:
                settings.preset = CUSTOM
    if args.overwrite:
        settings.skip_existing = False
    try:
        settings.validate()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.save:
        save_settings(settings)
        print(f"Saved defaults to {config_path()}")
    if args.show_config:
        for key, value in asdict(settings).items():
            print(f"{key:14} {value}")
        print(f"GPU encoder     {'available' if gpu_available(settings.codec) else 'not available'}")
        free = free_vram_mb()
        print(f"GPU free memory {f'{free} MB' if free is not None else 'unknown'}")
        return 0
    if not args.paths:
        if args.save:
            return 0
        build_parser().print_usage()
        return 2

    files = collect_videos(args.paths, args.recursive, settings.subfolder,
                           settings.output_dir or None)
    if not files:
        print("No .mp4/.mov videos found.", file=sys.stderr)
        return 1

    total = len(files)
    jobs = pick_jobs(files, settings)
    print(f"{total} video{'s' if total != 1 else ''}, converting {jobs} at a time.")
    active: dict[int, float] = {}  # index -> progress of files in flight
    finished = [0]
    lock = threading.Lock()

    def show():
        parts = [f"{files[i].name} {f * 100:3.0f}%" for i, f in sorted(active.items())]
        print(f"\r[{finished[0]}/{total}] " + " | ".join(parts).ljust(60),
              end="", flush=True)

    def on_progress(index, frac):
        with lock:
            active[index] = frac
            show()

    def on_result(index, res):
        with lock:
            active.pop(index, None)
            finished[0] += 1
            line = f"[{finished[0]}/{total}] {res.source.name}: {res.status}"
            if res.message:
                line += f" ({res.message})"
            print("\r" + line.ljust(70))

    cancel = threading.Event()
    try:
        results = convert_many(files, settings, None, on_progress, on_result, cancel,
                               jobs=jobs)
    except KeyboardInterrupt:
        cancel.set()
        print("\nCancelled.")
        return 130
    failed = sum(r.status == "failed" for r in results)
    done = sum(r.status == "done" for r in results)
    skipped = sum(r.status == "skipped" for r in results)
    print(f"\nDone: {done} converted, {skipped} skipped, {failed} failed.")
    return 1 if failed else 0

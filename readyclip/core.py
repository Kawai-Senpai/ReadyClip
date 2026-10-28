"""Conversion engine: probing, planning and running ffmpeg.

Everything here is UI-free so it can be used from the GUI, the CLI or your own
Python code:

    from readyclip import Settings, collect_videos, convert_many
    files = collect_videos(["D:/DCIM/100GOPRO"])
    convert_many(files, Settings(resolution=1080))
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Callable, Iterable

VIDEO_EXTS = {".mp4", ".mov"}

# Short-side pixel limits. 0 keeps the source resolution.
RESOLUTIONS = {
    0: "Original",
    2160: "4K (2160p)",
    1520: "2.7K (1520p)",
    1440: "1440p",
    1080: "1080p (Full HD)",
    720: "720p",
}

QUALITIES = {
    "high": "Best quality",
    "balanced": "Balanced",
    "small": "Small files",
}

CODECS = {
    "h264": "H.264 - plays everywhere",
    "hevc": "HEVC - half the size, newer phones",
}

FPS_CAPS = {0: "Keep original", 60: "60 fps max", 30: "30 fps max"}

# Files converted at the same time. 0 = decide from free GPU memory.
JOBS = {0: "Auto", 1: "1 at a time", 2: "2 at once", 3: "3 at once"}

# One-click settings. Limits checked October 2026: WhatsApp plays videos up to
# 16 MB inline (2 GB as a document), Telegram free accounts take 2 GB per file,
# Discord free uploads are 20 MB, most email providers cap attachments at 25 MB.
# Social sites re-encode whatever they get, so their presets send a clean 1080p
# H.264 file and let the site compress it. Every preset sets size_mode so
# switching presets never leaves "split" on for one that should shrink.
def _preset(resolution, quality, max_fps, max_size_mb=0, size_mode="shrink"):
    return dict(resolution=resolution, quality=quality, codec="h264",
                max_fps=max_fps, max_size_mb=max_size_mb, size_mode=size_mode)


PRESETS = {
    "phone": ("Phone-ready (recommended)", _preset(2160, "balanced", 60)),
    "whatsapp": ("WhatsApp video (under 16 MB)", _preset(1080, "balanced", 30, 16)),
    "whatsapp_doc": ("WhatsApp HD, send as document", _preset(1080, "high", 60, 2000)),
    "telegram": ("Telegram (full quality, 2 GB parts)",
                 _preset(2160, "balanced", 60, 2000, "split")),
    "social": ("Instagram / TikTok / Shorts", _preset(1080, "high", 30)),
    "youtube": ("YouTube (up to 4K)", _preset(2160, "high", 0)),
    "discord": ("Discord free (under 20 MB)", _preset(1080, "balanced", 30, 20)),
    "email": ("Email attachment (under 25 MB)", _preset(1080, "balanced", 30, 25)),
}
CUSTOM = "custom"

# What to do when a file would be bigger than max_size_mb.
SIZE_MODES = {
    "shrink": "Lower quality to fit",
    "split": "Split into parts (keeps quality)",
}

# (NVENC -cq, x264/x265 -crf) per codec and quality. Lower = better/bigger.
_QUALITY_TABLE = {
    "h264": {"high": (19, 18), "balanced": (23, 21), "small": (27, 25)},
    "hevc": {"high": (22, 20), "balanced": (26, 24), "small": (30, 28)},
}

# Longest side an encoder/phone can reasonably handle. NVENC H.264 tops out at
# 4096 wide, and phones will not play H.264 above 4K anyway.
_LONG_SIDE_CAP = {"h264": 4096, "hevc": 8192}

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass
class Settings:
    resolution: int = 2160
    quality: str = "balanced"
    codec: str = "h264"
    max_fps: int = 60
    use_gpu: bool = True
    subfolder: str = "converted"
    skip_existing: bool = True
    audio_kbps: int = 192
    jobs: int = 0
    max_size_mb: int = 0      # 0 = no limit
    size_mode: str = "shrink"  # SIZE_MODES key: how max_size_mb is enforced
    output_dir: str = ""      # "" = `subfolder` next to each source file
    preset: str = "phone"     # which PRESETS entry the values came from, or CUSTOM

    def validate(self) -> None:
        if self.jobs not in JOBS:
            raise ValueError(f"jobs must be one of {sorted(JOBS)}")
        if not 0 <= self.max_size_mb <= 100_000:
            raise ValueError("max_size_mb must be 0 (no limit) or a size in MB")
        if self.size_mode not in SIZE_MODES:
            raise ValueError(f"size_mode must be one of {list(SIZE_MODES)}")
        if self.preset not in PRESETS and self.preset != CUSTOM:
            raise ValueError(f"preset must be one of {list(PRESETS) + [CUSTOM]}")
        if self.resolution not in RESOLUTIONS:
            raise ValueError(f"resolution must be one of {sorted(RESOLUTIONS)}")
        if self.quality not in QUALITIES:
            raise ValueError(f"quality must be one of {list(QUALITIES)}")
        if self.codec not in CODECS:
            raise ValueError(f"codec must be one of {list(CODECS)}")
        if self.max_fps not in FPS_CAPS:
            raise ValueError(f"max_fps must be one of {sorted(FPS_CAPS)}")
        if not self.subfolder.strip() or any(c in self.subfolder for c in '\\/:*?"<>|'):
            raise ValueError("subfolder must be a plain folder name")


def apply_preset(settings: Settings, name: str) -> Settings:
    """A copy of `settings` with a preset's values filled in."""
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; choose from {list(PRESETS)}")
    return replace(settings, preset=name, **PRESETS[name][1])


def detect_preset(settings: Settings) -> str:
    """The preset whose values `settings` currently has, or CUSTOM."""
    for name, (_, values) in PRESETS.items():
        if all(getattr(settings, k) == v for k, v in values.items()):
            return name
    return CUSTOM


@dataclass
class Result:
    source: Path
    output: Path | None
    status: str  # "done", "skipped", "failed", "cancelled"
    message: str = ""


# ---------------------------------------------------------------- config

def config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "ReadyClip" / "config.json"


def load_settings() -> Settings:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as exc:
        print(f"ReadyClip: ignoring unreadable config ({exc})", file=sys.stderr)
        return Settings()
    known = {f.name for f in fields(Settings)}
    settings = Settings(**{k: v for k, v in data.items() if k in known})
    try:
        settings.validate()
    except ValueError as exc:
        print(f"ReadyClip: ignoring invalid config ({exc})", file=sys.stderr)
        return Settings()
    return settings


def save_settings(settings: Settings) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


# ---------------------------------------------------------------- ffmpeg

def find_tool(name: str) -> str:
    """Locate ffmpeg/ffprobe: next to the exe or package first, then PATH."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / exe)
    candidates.append(Path(__file__).resolve().parent.parent / exe)
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(
            f"{name} not found. Put {exe} next to ReadyClip or add it to PATH."
        )
    return found


_gpu_cache: dict[str, bool] = {}


def gpu_available(codec: str = "h264") -> bool:
    """True if an NVIDIA NVENC encoder for `codec` actually works here."""
    if codec not in _gpu_cache:
        cmd = [
            find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=black:s=320x240:d=0.2",
            "-c:v", f"{codec}_nvenc", "-f", "null", "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30,
                                  creationflags=_NO_WINDOW)
            _gpu_cache[codec] = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _gpu_cache[codec] = False
    return _gpu_cache[codec]


def free_vram_mb() -> int | None:
    """Free memory on the first NVIDIA GPU, or None if it cannot be read."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        return int(proc.stdout.split()[0]) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def vram_per_job_mb(pixels: int) -> int:
    """Rough GPU memory one full-GPU job needs for a source of this many pixels.

    Measured on an RTX 2060: ~2.4 GB for a 4K source, ~2.9 GB for 5.3K.
    Most of it is the CUDA context, decoder surfaces and encoder lookahead.
    """
    return int(1800 + 70 * pixels / 1e6)


def probe(path: Path) -> dict:
    cmd = [
        find_tool("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffprobe failed")
    data = json.loads(proc.stdout)
    video = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise RuntimeError("no video stream")
    audio = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "audio"), None)

    width, height = int(video["width"]), int(video["height"])
    rotation = 0
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = int(side["rotation"])
    rotation = int(video.get("tags", {}).get("rotate", rotation))
    if abs(rotation) % 180 == 90:  # ffmpeg auto-rotates before our filters
        width, height = height, width

    pix_fmt = video.get("pix_fmt", "")
    return {
        "width": width,
        "height": height,
        "fps": _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate")),
        "duration": float(data.get("format", {}).get("duration") or video.get("duration") or 0),
        "audio_codec": audio.get("codec_name") if audio else None,
        # GoPro records 10-bit, full-range BT.709. Carrying these through is
        # what keeps the converted video looking like the original.
        "ten_bit": any(b in pix_fmt for b in ("10", "12", "p010", "p016")),
        "full_range": video.get("color_range") == "pc" or pix_fmt.startswith("yuvj"),
        "color_primaries": _known(video.get("color_primaries"), "bt709"),
        "color_trc": _known(video.get("color_transfer"), "bt709"),
        "colorspace": _known(video.get("color_space"), "bt709"),
    }


def _known(value: str | None, default: str) -> str:
    return value if value and value not in ("unknown", "reserved") else default


def _parse_rate(rate: str | None) -> float:
    try:
        num, den = (rate or "0/1").split("/")
        return float(num) / float(den) if float(den) else 0.0
    except ValueError:
        return 0.0


def target_size(width: int, height: int, settings: Settings) -> tuple[int, int]:
    """Scale down (never up) so the short side fits the chosen resolution."""
    short, long_ = min(width, height), max(width, height)
    factor = 1.0
    if settings.resolution:
        factor = min(factor, settings.resolution / short)
    factor = min(factor, _LONG_SIDE_CAP[settings.codec] / long_)
    even = lambda v: max(2, int(round(v / 2)) * 2)  # noqa: E731
    return even(width * factor), even(height * factor)


def output_path(src: Path, settings: Settings) -> Path:
    if settings.output_dir:
        return Path(settings.output_dir) / (src.stem + ".mp4")
    return src.parent / settings.subfolder / (src.stem + ".mp4")


def plan_outputs(files: list[Path], settings: Settings) -> list[Path]:
    """Output path per file. With one shared output folder, clips from
    different cards often share a name (GX010001.MP4), so later ones get the
    source folder's name appended instead of overwriting or being skipped."""
    taken: set[str] = set()
    outputs = []
    for src in files:
        dst = output_path(src, settings)
        n = 1
        while str(dst).lower() in taken:
            suffix = f"_{src.parent.name}" + (f"_{n}" if n > 1 else "")
            dst = dst.with_name(f"{src.stem}{suffix}.mp4")
            n += 1
        taken.add(str(dst).lower())
        outputs.append(dst)
    return outputs


# Short sides tried, largest first, when a size limit forces a lower resolution.
_SIZE_LADDER = (2160, 1440, 1080, 720, 540, 480, 360)
# Below this many bits per pixel per frame the picture turns to mush, so drop
# resolution instead. HEVC needs fewer bits for the same quality.
_MIN_BPP = {"h264": 0.05, "hevc": 0.035}
_MIN_VIDEO_KBPS = 150
_SIZE_MARGIN = 0.95  # leave room for container overhead and encoder overshoot


def plan_size_limit(info: dict, settings: Settings) -> tuple[Settings, int] | str:
    """Fit a clip under settings.max_size_mb.

    Returns (settings to encode with, video bitrate cap in kbps), or an error
    message when the clip is too long to fit at any usable quality. Lowers the
    frame rate to 30 and then the resolution only as far as the budget needs.
    """
    duration = info["duration"]
    if duration <= 0:
        return "cannot fit a size limit: the video's length is unknown"
    budget = settings.max_size_mb * 1_000_000 * 8 * _SIZE_MARGIN / duration / 1000
    audio = 0
    if info["audio_codec"]:
        audio = 128 if budget >= 1500 else 96 if budget >= 600 else 64
    video = int(budget - audio)
    if video < _MIN_VIDEO_KBPS:
        need = (_MIN_VIDEO_KBPS + max(audio, 64)) * 1000 * duration / 8 / 1_000_000 / _SIZE_MARGIN
        return (f"{duration / 60:.1f} min is too long for {settings.max_size_mb} MB; "
                f"it needs at least {need:.0f} MB. Trim it or raise the limit.")

    max_fps = settings.max_fps
    if video < 5000 and info["fps"] > 30.5 and (max_fps == 0 or max_fps > 30):
        max_fps = 30
    out_fps = min(info["fps"], max_fps) if max_fps else info["fps"]
    out_fps = out_fps or 30

    top = min(settings.resolution or 10**6, info["width"], info["height"])
    candidates = [top] + [s for s in _SIZE_LADDER if s < top]
    chosen = candidates[-1]
    for short in candidates:
        w, h = target_size(info["width"], info["height"], replace(settings, resolution=short))
        if video * 1000 / (w * h * out_fps) >= _MIN_BPP[settings.codec]:
            chosen = short
            break
    return replace(settings, resolution=chosen, max_fps=max_fps, audio_kbps=audio), video


# Tried in this order; a mode that fails falls through to the next one.
#   cuda:   decode, resize and encode all on the GPU (fastest, ~1.8x hybrid)
#   hybrid: GPU decode/encode, resize on the CPU (works for more sources)
#   cpu:    libx264/libx265, no NVIDIA GPU needed
GPU_MODES = ("cuda", "hybrid", "cpu")


def build_command(src: Path, dst: Path, info: dict, settings: Settings,
                  mode: str, max_kbps: int | None = None,
                  keyframe_secs: float | None = None,
                  faststart: bool = True) -> list[str]:
    """ffmpeg arguments for one conversion. `max_kbps` caps the video bitrate
    (size-limit mode) on top of the usual quality target. `keyframe_secs`
    forces regular keyframes so the result can be split precisely.
    `faststart` moves the index to the front for instant playback; it costs a
    full rewrite of the file, so it is skipped when the file will be split."""
    cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y",
           "-loglevel", "error"]
    if mode == "cuda":
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    elif mode == "hybrid":
        # Decode on the GPU when possible; ffmpeg falls back to CPU otherwise.
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?",  # drop GoPro telemetry/timecode tracks
            "-map_metadata", "0", "-dn", "-sn"]

    # 4:2:0, what phones decode in hardware. HEVC keeps 10-bit sources at
    # 10-bit (no banding in skies; phones that play HEVC handle Main10).
    # H.264 must be 8-bit: phone decoders do not support 10-bit H.264.
    # The colour range is kept as recorded: GoPro uses full range, and the
    # CPU scaler's silent full->limited conversion measured 6 dB worse.
    ten_bit = info.get("ten_bit") and settings.codec == "hevc"
    full = info.get("full_range", False)
    filters = []
    w, h = target_size(info["width"], info["height"], settings)
    if mode == "cuda":
        # scale_cuda's lanczos uses a fixed-size kernel, so big shrinks alias
        # (5.3K->1080p: 34.9 dB vs 36.5 for the CPU scaler). Halving first
        # with bilinear, an exact 2x2 average, then one final lanczos step
        # measured 36.7 dB: as sharp as the CPU and still all on the GPU.
        cw, ch = info["width"], info["height"]
        while cw > 2 * w and ch > 2 * h:
            cw, ch = cw // 4 * 2, ch // 4 * 2
            filters.append(f"scale_cuda={cw}:{ch}:interp_algo=bilinear")
        fmt = "p010le" if ten_bit else "yuv420p"
        filters.append(f"scale_cuda={w}:{h}:interp_algo=lanczos:format={fmt}")
    else:
        rng = "full" if full else "limited"
        filters.append(f"scale={w}:{h}:flags=lanczos:in_range={rng}:out_range={rng}")
    if settings.max_fps and info["fps"] > settings.max_fps + 0.5:
        filters.append(f"fps={settings.max_fps}")
    if mode != "cuda":
        filters.append("format=" + ("yuv420p10le" if ten_bit else "yuv420p"))
    cmd += ["-vf", ",".join(filters),
            "-color_range", "pc" if full else "tv",
            "-color_primaries", info.get("color_primaries", "bt709"),
            "-color_trc", info.get("color_trc", "bt709"),
            "-colorspace", info.get("colorspace", "bt709")]

    # Quality-targeted (CQ/CRF). With a size limit the same target is capped by
    # maxrate, so easy clips stay small and hard ones still fit. NVENC needs
    # -b:v 0 here: a -b:v equal to the cap becomes a target and bloats files
    # (a 2 GB cap made a 104 MB clip 177 MB in testing).
    cq, crf = _QUALITY_TABLE[settings.codec][settings.quality]
    cap = (["-maxrate", f"{max_kbps}k", "-bufsize", f"{max_kbps * 2}k"]
           if max_kbps else [])
    if mode != "cpu":
        cmd += ["-c:v", f"{settings.codec}_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(cq), "-b:v", "0", *cap,
                "-spatial-aq", "1", "-temporal-aq", "1", "-rc-lookahead", "20"]
    elif settings.codec == "h264":
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf), *cap]
    else:
        cmd += ["-c:v", "libx265", "-preset", "medium", "-crf", str(crf), *cap,
                "-x265-params", "log-level=error"]
    if settings.codec == "h264":
        cmd += ["-profile:v", "high"]
    else:  # hvc1 is required by iOS
        cmd += ["-profile:v", "main10" if ten_bit else "main", "-tag:v", "hvc1"]
    if keyframe_secs:
        out_fps = min(info["fps"], settings.max_fps) if settings.max_fps else info["fps"]
        cmd += ["-g", str(max(1, round((out_fps or 30) * keyframe_secs)))]

    if info["audio_codec"] == "aac" and not max_kbps:
        cmd += ["-c:a", "copy"]
    elif info["audio_codec"]:
        cmd += ["-c:a", "aac", "-b:a", f"{settings.audio_kbps}k", "-ac", "2"]

    # -write_tmcd 0: GoPro sources carry a timecode that the mp4 muxer would
    # otherwise turn into a data track (despite -dn), which breaks splitting.
    if faststart:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-write_tmcd", "0", "-progress", "pipe:1", "-nostats", "-f", "mp4", str(dst)]
    return cmd


# ---------------------------------------------------------------- converting

ProgressFn = Callable[[float], None]


def collect_videos(paths: Iterable[str | Path], recursive: bool = False,
                   subfolder: str = "converted",
                   output_dir: str | Path | None = None) -> list[Path]:
    """Expand files and folders into a sorted list of video files.

    Our own output folders are skipped so re-running never re-converts.
    """
    out_root = Path(output_dir).resolve() if output_dir else None
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            if p.suffix.lower() in VIDEO_EXTS:
                found.append(p)
        elif p.is_dir():
            walker = p.rglob("*") if recursive else p.iterdir()
            for f in walker:
                if (f.is_file() and f.suffix.lower() in VIDEO_EXTS
                        and not f.name.startswith(".")
                        and subfolder not in f.relative_to(p).parts[:-1]):
                    found.append(f)
    seen, unique = set(), []
    for f in sorted(found, key=lambda x: str(x).lower()):
        resolved = f.resolve()
        if out_root and out_root in resolved.parents:
            continue
        key = str(resolved).lower()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def convert_file(src: str | Path, settings: Settings | None = None,
                 on_progress: ProgressFn | None = None,
                 cancel: threading.Event | None = None,
                 dst: str | Path | None = None) -> Result:
    """Convert one file. Writes to a temp name and renames only on success.

    `dst` overrides the output path (convert_many uses it to avoid name
    clashes in a shared output folder).
    """
    src = Path(src)
    settings = settings or Settings()
    settings.validate()
    dst = Path(dst) if dst else output_path(src, settings)
    split = bool(settings.max_size_mb) and settings.size_mode == "split"
    limit_bytes = settings.max_size_mb * 1_000_000
    if settings.skip_existing and _part_path(dst, 1).exists():
        return Result(src, _part_path(dst, 1), "skipped", "already split")
    if (settings.skip_existing and split and dst.exists()
            and dst.stat().st_size > limit_bytes):
        # Already converted but too big: just cut it, no need to re-encode.
        return _split_existing(src, dst, limit_bytes, cancel)
    if settings.skip_existing and dst.exists():
        return Result(src, dst, "skipped", "already converted")

    try:
        info = probe(src)
    except (RuntimeError, ValueError, KeyError) as exc:
        return Result(src, None, "failed", f"cannot read video: {exc}")
    encode_settings, max_kbps = settings, None
    if settings.max_size_mb and not split:
        plan = plan_size_limit(info, settings)
        if isinstance(plan, str):
            return Result(src, None, "failed", plan)
        encode_settings, max_kbps = plan
    shrink_bytes = 0 if split else limit_bytes  # only shrink mode re-encodes to fit

    dst.parent.mkdir(parents=True, exist_ok=True)
    # Check space before a long encode rather than failing near the end. The
    # output is rarely bigger than the source; splitting briefly needs the
    # full file and its parts side by side.
    need = src.stat().st_size
    if shrink_bytes:
        need = min(need, shrink_bytes)
    no_space = _check_space(dst.parent, need * (2 if split else 1))
    if no_space:
        return Result(src, None, "failed", no_space)
    tmp = dst.with_name(dst.stem + ".part.mp4")
    use_gpu = settings.use_gpu and gpu_available(settings.codec)
    modes = GPU_MODES if use_gpu else ("cpu",)

    # A generous cap is worse than none: NVENC has its own internal ceiling, and
    # an explicit huge one lifts it (a 2 GB cap made a 104 MB clip 177 MB).
    # So only cap when the budget is tight; the retry below caps if needed.
    first_kbps = max_kbps
    if max_kbps:
        w, h = target_size(info["width"], info["height"], encode_settings)
        fps = (min(info["fps"], encode_settings.max_fps) if encode_settings.max_fps
               else info["fps"]) or 30
        if max_kbps * 1000 > 0.35 * w * h * fps:
            first_kbps = None

    def encode(mode: str) -> tuple[str, str, bool]:
        """Run one encoder mode; returns (status, message, missed_size_limit)."""
        kbps = first_kbps
        for _ in range(3):  # retries only happen when a size limit is overshot
            status, message = _run(
                build_command(src, tmp, info, encode_settings, mode, kbps,
                              keyframe_secs=2 if split else None, faststart=not split),
                info["duration"], on_progress, cancel)
            if status != "done" or not shrink_bytes:
                return status, message, False
            size = tmp.stat().st_size
            if size <= shrink_bytes:
                return status, message, False
            if kbps is None:  # uncapped try overshot: now apply the planned cap
                kbps = max_kbps
            else:  # capped try overshot: aim proportionally lower
                kbps = max(_MIN_VIDEO_KBPS, int(kbps * shrink_bytes / size * 0.92))
        return "failed", (f"could not get under {settings.max_size_mb} MB "
                          f"(last try {size / 1_000_000:.1f} MB)"), True

    for attempt, mode in enumerate(modes):
        status, message, size_miss = encode(mode)
        # Fall through to the next encoder only on errors; a size miss would
        # not improve with a different encoder.
        if status != "failed" or size_miss:
            break

    if status != "done":
        tmp.unlink(missing_ok=True)
        return Result(src, None, status, message)

    notes = [{"cuda": "GPU", "hybrid": "GPU, CPU resize", "cpu": "CPU"}[mode]]
    if attempt:
        notes.append(f"fell back from {modes[0]}")
    os.replace(tmp, dst)
    if split and dst.stat().st_size > limit_bytes:
        parts = split_file(dst, dst, info["duration"], limit_bytes, cancel)
        if isinstance(parts, str):
            # Keep the converted file: re-encoding can take far longer than
            # splitting, and the next split-mode run will cut this file directly.
            return Result(src, dst, "failed",
                          f"converted, but splitting failed (full file kept): {parts}")
        dst.unlink()
        notes.append(f"split into {len(parts)} parts")
        return Result(src, parts[0], "done", ", ".join(notes))
    if shrink_bytes:
        w, h = target_size(info["width"], info["height"], encode_settings)
        notes.append(f"{dst.stat().st_size / 1_000_000:.1f} MB at {min(w, h)}p")
    return Result(src, dst, "done", ", ".join(notes))


def _part_path(dst: Path, n: int) -> Path:
    return dst.with_name(f"{dst.stem}_part{n}.mp4")


def _check_space(folder: Path, need_bytes: int) -> str | None:
    """An error message if `folder`'s drive has less than `need_bytes` free."""
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return None  # cannot tell; ffmpeg will report a full disk itself
    need_bytes += 200_000_000  # headroom for temp files and the filesystem
    if free >= need_bytes:
        return None
    return (f"not enough free space on {folder.anchor or folder}: needs about "
            f"{need_bytes / 1e9:.1f} GB, {free / 1e9:.1f} GB free")


def _split_existing(src: Path, dst: Path, limit_bytes: int,
                    cancel: threading.Event | None) -> Result:
    """Split an earlier full-size output in place of converting again."""
    no_space = _check_space(dst.parent, dst.stat().st_size)
    if no_space:
        return Result(src, dst, "failed", no_space)
    try:
        duration = probe(dst)["duration"]
    except (RuntimeError, ValueError, KeyError) as exc:
        return Result(src, dst, "failed", f"cannot read existing output: {exc}")
    parts = split_file(dst, dst, duration, limit_bytes, cancel)
    if isinstance(parts, str):
        return Result(src, dst, "failed", f"splitting the existing output failed: {parts}")
    dst.unlink()
    return Result(src, parts[0], "done",
                  f"split existing output into {len(parts)} parts, no re-encode")


def split_file(src: Path, dst: Path, duration: float, limit_bytes: int,
               cancel: threading.Event | None = None) -> list[Path] | str:
    """Cut `src` into equal-length parts named <dst>_part1.mp4, ... that are each
    under `limit_bytes`, without re-encoding. Cuts land on keyframes, so parts
    vary a little; if one is still too big, try again with more parts."""
    size = src.stat().st_size
    count = max(2, math.ceil(size / (limit_bytes * 0.92)))
    pattern = dst.with_name(f"{dst.stem}_split%d.tmp.mp4")
    for _ in range(4):
        # Only video + first audio: older outputs can hold a timecode (tmcd)
        # track that the mp4 segment muxer refuses ("codec none").
        cmd = [find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
               "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-dn", "-c", "copy",
               "-map_metadata", "0",
               "-f", "segment", "-segment_time", f"{duration / count:.3f}",
               "-segment_start_number", "1", "-reset_timestamps", "1",
               "-segment_format", "mp4",
               "-segment_format_options", "movflags=+faststart:write_tmcd=0",
               str(pattern)]
        status, message = _run(cmd, 0, None, cancel)
        prefix = f"{dst.stem}_split"  # not glob(): "[" in names is a pattern there
        made = sorted((p for p in dst.parent.iterdir()
                       if p.name.startswith(prefix) and p.name.endswith(".tmp.mp4")
                       and p.name[len(prefix):-8].isdigit()),
                      key=lambda p: int(p.name[len(prefix):-8]))
        if status == "done" and made and all(p.stat().st_size <= limit_bytes for p in made):
            parts = []
            for n, p in enumerate(made, 1):
                final = _part_path(dst, n)
                os.replace(p, final)
                parts.append(final)
            n = len(parts) + 1
            while _part_path(dst, n).exists():  # leftovers from an earlier, longer split
                _part_path(dst, n).unlink()
                n += 1
            return parts
        for p in made:
            p.unlink(missing_ok=True)
        if status != "done":
            return message
        count += 1
    return f"could not split into parts under {limit_bytes // 1_000_000} MB"


def _run(cmd: list[str], duration: float, on_progress: ProgressFn | None,
         cancel: threading.Event | None) -> tuple[str, str]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=_NO_WINDOW)
    errors: list[str] = []
    reader = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
    reader.start()

    for line in proc.stdout:
        if cancel is not None and cancel.is_set():
            proc.kill()
            break
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and duration > 0 and on_progress:
            try:
                on_progress(min(1.0, max(0.0, int(value) / 1e6 / duration)))
            except ValueError:
                pass  # "N/A" before the first frame is written
    proc.wait()
    reader.join(timeout=5)

    if cancel is not None and cancel.is_set():
        return "cancelled", "cancelled"
    if proc.returncode != 0:
        tail = "".join(errors[-5:]).strip() or f"ffmpeg exit code {proc.returncode}"
        if "No space left" in tail or "There is not enough space" in tail:
            tail = "the output drive ran out of space"
        elif "File too large" in tail:
            tail = ("the output drive cannot hold files this big (FAT32 stops at 4 GB); "
                    "save to another drive or turn on splitting at 4000 MB")
        return "failed", tail
    if on_progress:
        on_progress(1.0)
    return "done", ""


def pick_jobs(files: list[Path], settings: Settings) -> int:
    """How many files to convert at once.

    GPU memory is the limit, not the encoder: on a 6 GB card one 5.3K job
    uses ~2.9 GB, and overcommitting made each file 2-16x slower in testing.
    Auto only runs extra jobs when free memory fits all of them, and stays
    at 1 for CPU encoding (x264/x265 already use every core).
    """
    if settings.jobs:
        return settings.jobs
    if len(files) < 2 or not (settings.use_gpu and gpu_available(settings.codec)):
        return 1
    free = free_vram_mb()
    if free is None:
        return 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        infos = list(pool.map(_try_probe, files))
    sizes = [i["width"] * i["height"] for i in infos if i]
    if not sizes:
        return 1
    need = vram_per_job_mb(max(sizes))
    return max(1, min(3, (free - 512) // need))  # keep ~0.5 GB for the desktop


def _try_probe(path: Path) -> dict | None:
    try:
        return probe(path)
    except (RuntimeError, ValueError, KeyError):
        return None


def convert_many(files: Iterable[str | Path], settings: Settings | None = None,
                 on_file: Callable[[int, Path], None] | None = None,
                 on_progress: Callable[[int, float], None] | None = None,
                 on_result: Callable[[int, Result], None] | None = None,
                 cancel: threading.Event | None = None,
                 jobs: int | None = None) -> list[Result]:
    """Convert several files, running `jobs` of them at once.

    Callbacks get the file's index in `files` and may be called from worker
    threads. `jobs=None` uses settings.jobs (0 = pick from free GPU memory).
    Results come back in input order; files never started after a cancel
    are left out.
    """
    files = [Path(f) for f in files]
    settings = settings or Settings()
    settings.validate()
    cancel = cancel or threading.Event()
    outputs = plan_outputs(files, settings)
    if settings.use_gpu:
        gpu_available(settings.codec)  # fill the cache before threads race on it
    workers = jobs or pick_jobs(files, settings)

    def one(index: int) -> Result | None:
        if cancel.is_set():
            return None
        if on_file:
            on_file(index, files[index])
        progress = (lambda frac: on_progress(index, frac)) if on_progress else None
        result = convert_file(files[index], settings, progress, cancel, outputs[index])
        if on_result:
            on_result(index, result)
        return result

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        results = list(pool.map(one, range(len(files))))
    except BaseException:  # Ctrl+C or a callback error: stop the running ffmpegs too
        cancel.set()
        raise
    finally:
        pool.shutdown(wait=True)
    return [r for r in results if r is not None]

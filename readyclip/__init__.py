"""ReadyClip: turn action-cam footage into phone-friendly, shareable MP4s."""

__version__ = "1.0.0"
APP_NAME = "ReadyClip"

from readyclip.core import (  # noqa: E402
    PRESETS,
    Settings,
    Result,
    apply_preset,
    collect_videos,
    convert_file,
    convert_many,
    gpu_available,
    load_settings,
    pick_jobs,
    save_settings,
)

__all__ = [
    "APP_NAME",
    "PRESETS",
    "Settings",
    "Result",
    "apply_preset",
    "collect_videos",
    "convert_file",
    "convert_many",
    "gpu_available",
    "load_settings",
    "pick_jobs",
    "save_settings",
    "__version__",
]

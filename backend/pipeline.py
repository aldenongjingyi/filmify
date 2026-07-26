"""
pipeline.py

Builds and runs the FFmpeg command chain that turns a raw iPhone clip
into a graded, cropped, grained, Instagram-ready output.

Everything here is self-contained (no external LUT files or grain
overlay assets required) — grading is done with FFmpeg's native
color filters, and grain is synthesized with the `noise` filter.
This keeps the MVP free of licensing/asset-management concerns.
"""

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path


# ---- Look presets -----------------------------------------------------
# Each preset is a chain of native FFmpeg filters. No external .cube
# files needed. Tune these to taste — they're deliberately conservative
# starting points.

LOOK_PRESETS = {
    "warm_film": (
        "eq=saturation=0.92:contrast=1.06:brightness=0.01,"
        "colorbalance=rs=0.06:gs=0.01:bs=-0.06:rm=0.03:gm=0:bm=-0.03:"
        "rh=0.02:gh=0:bh=-0.02,"
        "curves=r='0/0.03 0.5/0.52 1/0.98':b='0/0 0.5/0.47 1/0.95'"
    ),
    "teal_orange": (
        "eq=saturation=1.05:contrast=1.1,"
        "colorbalance=rs=0.1:gs=0.02:bs=-0.12:rm=0.08:gm=0:bm=-0.1:"
        "rh=0.1:gh=0.02:bh=-0.1"
    ),
    "faded": (
        "eq=saturation=0.78:contrast=0.92:brightness=0.02,"
        "curves=all='0/0.08 0.5/0.5 1/0.9'"
    ),
}

ASPECT_RATIOS = {
    # width:height crop ratios. Source is cropped to these before scale.
    "instagram_landscape": 1.91,  # 1080x566
    "widescreen": 16 / 9,
    "cinematic": 2.39,
}

EXPORT_DIMENSIONS = {
    "instagram_landscape": (1080, 566),
    "widescreen": (1080, 608),
    "cinematic": (1080, 452),
}


@dataclass
class JobOptions:
    look: str = "warm_film"
    aspect_ratio: str = "instagram_landscape"
    grain_intensity: int = 15       # 0-40, maps to FFmpeg noise strength
    target_fps: int = 24
    denoise_audio: bool = True
    ambient_bed_path: str | None = None  # optional audio bed to mix in


def build_filter_chain(options: JobOptions) -> str:
    ratio = ASPECT_RATIOS[options.aspect_ratio]
    out_w, out_h = EXPORT_DIMENSIONS[options.aspect_ratio]
    look_filter = LOOK_PRESETS[options.look]
    grain = max(0, min(options.grain_intensity, 40))

    filters = [
        f"fps={options.target_fps}",
        f"crop=iw:iw/{ratio:.4f}",
        look_filter,
        # Film grain: Gaussian (averaged) distribution, luma-dominant.
        # c0=luma gets the bulk of grain; c1/c2 (chroma) get ~1/8 —
        # matching how silver-halide grain is almost entirely a luminance
        # phenomenon. t=temporal (changes per frame), a=averaged samples
        # (approximates Gaussian vs the harsh uniform distribution of 'u').
        f"noise=c0s={grain}:c0f=t+a:c1s={max(1, grain // 8)}:c1f=t+a:c2s={max(1, grain // 8)}:c2f=t+a",
        f"scale={out_w}:{out_h}:flags=lanczos",
    ]
    return ",".join(filters)


def build_audio_filter(options: JobOptions) -> str | None:
    if options.denoise_audio:
        return "afftdn=nf=-25"
    return None


async def run_pipeline(input_path: Path, output_path: Path, options: JobOptions,
                        on_progress=None) -> None:
    """
    Runs the FFmpeg command as an async subprocess so the FastAPI event
    loop stays responsive. Raises RuntimeError with stderr on failure.
    """
    vf = build_filter_chain(options)
    af = build_audio_filter(options)

    cmd = ["ffmpeg", "-y", "-i", str(input_path)]

    if options.ambient_bed_path:
        cmd += ["-i", options.ambient_bed_path]
        audio_graph = "[0:a]afftdn=nf=-25[clean];" \
                       "[1:a]volume=0.15[amb];" \
                       "[clean][amb]amix=inputs=2:duration=first[aout]"
        cmd += ["-filter_complex", audio_graph, "-map", "0:v", "-map", "[aout]"]
        cmd += ["-vf", vf]
    else:
        cmd += ["-vf", vf]
        if af:
            cmd += ["-af", af]

    cmd += [
        "-r", str(options.target_fps),
        "-c:v", "libx264", "-b:v", "9M", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='ignore')[-2000:]}")

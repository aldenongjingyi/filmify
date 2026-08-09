"""
pipeline.py — cinema-quality FFmpeg processing pipeline

Processing order:
  1. FPS conversion + tblend  — 24fps with 180° shutter motion-blur simulation
  2. deflicker                — fix iPhone auto-exposure frame-to-frame drift
  3. lenscorrection           — compensate iPhone barrel/wide-angle distortion
  4. unsharp (negative)       — remove computational phone sharpness / diffusion
  5. colour grade             — bold S-curve (toe+shoulder), split-toning per preset
  6. halation                 — warm highlight bloom (film-base light scatter)
  7. vignette                 — lens corner light falloff
  8. scale                    — Lanczos to 1080px wide, original aspect ratio preserved
  9. film grain               — half-res Gaussian noise → neighbour upscale (clumping)

Audio:
  - afftdn noise reduction
  - acompressor (cinematic dynamics)
  - alimiter (prevent clipping)
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Look presets
# Each is a chain of comma-separated FFmpeg filters embedded in filter_complex.
# Deliberately bold — conservative grades just look "edited", not cinematic.
# ---------------------------------------------------------------------------

LOOK_PRESETS = {
    "warm_film": (
        # S-curve: toe lifts blacks off pure black, shoulder rolls off highlights
        # softly. Red pushed warm throughout; blue pulled back. Colorbalance
        # adds amber to shadows/mids (Kodak Vision3 signature).
        "curves=all='0/0.04 0.15/0.16 0.5/0.51 0.85/0.87 1/0.96':"
        "r='0/0.05 0.5/0.53 1/0.97':"
        "b='0/0.02 0.5/0.47 1/0.93',"
        "eq=saturation=0.88:contrast=1.04,"
        "colorbalance=rs=0.08:gs=0.02:bs=-0.08:"
        "rm=0.04:gm=0.01:bm=-0.03:"
        "rh=-0.01:gh=0.01:bh=0.01"
    ),
    "teal_orange": (
        # Blockbuster split: teal/green pushed into shadows, orange into mids,
        # warm highlights. High contrast + saturation boost for punchy colour.
        "curves=all='0/0.02 0.15/0.12 0.5/0.5 0.85/0.88 1/0.97',"
        "eq=saturation=1.12:contrast=1.15,"
        "colorbalance=rs=-0.12:gs=0.04:bs=0.10:"
        "rm=0.10:gm=0.01:bm=-0.12:"
        "rh=0.08:gh=0.02:bh=-0.06"
    ),
    "faded": (
        # Heavy toe lift = matte blacks. Compressed highlights. Strong desaturation.
        # Blue-green tint in shadows mimics expired/pushed film stock.
        "curves=all='0/0.10 0.30/0.30 0.70/0.68 1/0.88',"
        "eq=saturation=0.72:contrast=0.88,"
        "colorbalance=rs=-0.03:gs=0.02:bs=0.06:"
        "rm=-0.01:gm=0.01:bm=0.03"
    ),
}

@dataclass
class JobOptions:
    look: str = "warm_film"
    grain_intensity: int = 15       # 0–40
    target_fps: int = 24
    denoise_audio: bool = True
    ambient_bed_path: str | None = None


def build_filter_complex(options: JobOptions) -> tuple[str, str]:
    """
    Builds the FFmpeg filter_complex graph string.
    Returns (filter_complex_string, output_video_label).

    Aspect ratio is never altered — original framing is preserved.
    Output is scaled to 1080px wide (height calculated automatically).
    """
    grain = max(0, min(options.grain_intensity, 40))
    grade = LOOK_PRESETS[options.look]
    chroma_grain = max(1, grain // 8)

    segs = []

    # 1. FPS conversion + motion-blur simulation
    segs.append(
        f"[0:v]fps={options.target_fps},"
        f"tblend=all_mode=average:all_opacity=0.18"
        f"[fps_out]"
    )

    # 2. Deflicker
    segs.append("[fps_out]deflicker=size=5:mode=am[deflickered]")

    # 3. Lens correction — iPhone barrel distortion compensation
    segs.append("[deflickered]lenscorrection=k1=-0.10:k2=0.03[lc]")

    # 4. Diffusion — remove computational phone sharpness
    segs.append("[lc]unsharp=5:5:-0.30:5:5:0[soft]")

    # 5. Colour grade
    segs.append(f"[soft]{grade}[graded]")

    # 6. Halation
    segs.append("[graded]split=2[main][bloom_src]")
    segs.append(
        "[bloom_src]"
        "curves=all='0/0 0.65/0 1/1',"
        "colorbalance=rs=0.35:gs=-0.05:bs=-0.25,"
        "gblur=sigma=18"
        "[bloom]"
    )
    segs.append("[main][bloom]blend=all_mode=screen:all_opacity=0.20[halated]")

    # 7. Vignette
    segs.append("[halated]vignette=PI/4[vignetted]")

    # 8. Scale to 1080px wide, preserve original aspect ratio.
    #    -2 ensures height is divisible by 2 (required by libx264).
    segs.append("[vignetted]scale=1080:-2:flags=lanczos[scaled]")

    # 9. Film grain — half-res then neighbour-upscale for grain clumping
    if grain > 0:
        segs.append("[scaled]split=2[base][grain_src]")
        segs.append(
            f"[grain_src]"
            f"scale=iw/2:ih/2,"
            f"noise=c0s={grain}:c0f=t+a:c1s={chroma_grain}:c1f=t+a:c2s={chroma_grain}:c2f=t+a,"
            f"scale=iw*2:ih*2:flags=neighbor"
            f"[grain_layer]"
        )
        segs.append("[base][grain_layer]blend=all_mode=overlay:all_opacity=0.12[out]")
        out_label = "[out]"
    else:
        out_label = "[scaled]"

    return ";".join(segs), out_label


def build_audio_filters(options: JobOptions) -> list[str]:
    filters = []
    if options.denoise_audio:
        # FFT-based broadband noise reduction
        filters.append("afftdn=nf=-25")
    # Cinematic dynamic compression: gentle ratio, slow attack preserves
    # transient punch, 1.5x makeup gain brings level up.
    filters.append("acompressor=threshold=0.089:ratio=3:attack=5:release=50:makeup=1.5")
    # Hard limiter prevents clipping from the makeup gain
    filters.append("alimiter=level_out=0.9:limit=0.9:attack=5:release=50")
    return filters


async def run_pipeline(
    input_path: Path,
    output_path: Path,
    options: JobOptions,
    on_progress=None,
) -> None:
    """
    Runs FFmpeg as an async subprocess so the FastAPI event loop stays
    responsive during processing. Raises RuntimeError on FFmpeg failure.
    """
    fc, vid_label = build_filter_complex(options)
    audio_filters = build_audio_filters(options)
    af_str = ",".join(audio_filters)

    cmd = ["ffmpeg", "-y", "-i", str(input_path)]

    if options.ambient_bed_path:
        cmd += ["-i", options.ambient_bed_path]
        audio_graph = (
            f"[0:a]{af_str}[clean];"
            "[1:a]volume=0.15[amb];"
            "[clean][amb]amix=inputs=2:duration=first[aout]"
        )
        full_fc = fc + ";" + audio_graph
        cmd += ["-filter_complex", full_fc, "-map", vid_label, "-map", "[aout]"]
    else:
        cmd += ["-filter_complex", fc, "-map", vid_label, "-map", "0:a?"]
        cmd += ["-af", af_str]

    cmd += [
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
        raise RuntimeError(
            f"ffmpeg failed:\n{stderr.decode(errors='ignore')[-3000:]}"
        )

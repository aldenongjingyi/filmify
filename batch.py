"""
batch.py — local batch processor for Filmify

Runs the pipeline directly (no HTTP) so there's no upload overhead.
Processes clips concurrently up to WORKERS at a time.

Usage:
    python batch.py "/path/to/folder" [--look warm_film] [--aspect cinematic] [--grain 18] [--workers 3]
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Make sure backend is importable
sys.path.insert(0, str(Path(__file__).parent / "backend"))
from pipeline import JobOptions, run_pipeline  # noqa: E402

VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mts", ".m2ts", ".avi", ".3gp", ".MOV", ".MP4", ".MTS", ".M2TS", ".AVI"}

RESET  = "\033[0m"
GREEN  = "\033[32m"
GOLD   = "\033[33m"
RED    = "\033[31m"
DIM    = "\033[2m"
BOLD   = "\033[1m"


def find_clips(folder: Path) -> list[Path]:
    clips = []
    for p in sorted(folder.rglob("*")):
        if p.suffix in VIDEO_EXTENSIONS and p.is_file():
            clips.append(p)
    return clips


async def process_one(clip: Path, out_dir: Path, options: JobOptions, sem: asyncio.Semaphore, idx: int, total: int):
    out_path = out_dir / f"{clip.stem}_filmify.mp4"

    # Skip if already done
    if out_path.exists():
        print(f"{DIM}[{idx}/{total}] SKIP  {clip.name} (already exists){RESET}")
        return True

    async with sem:
        print(f"{GOLD}[{idx}/{total}] START {clip.name}{RESET}")
        try:
            await run_pipeline(clip, out_path, options)
            print(f"{GREEN}[{idx}/{total}] DONE  {clip.name}  →  {out_path.name}{RESET}")
            return True
        except Exception as e:
            print(f"{RED}[{idx}/{total}] ERROR {clip.name}: {e}{RESET}")
            out_path.unlink(missing_ok=True)
            return False


async def main():
    parser = argparse.ArgumentParser(description="Filmify batch processor")
    parser.add_argument("folder", help="Folder containing source clips (searched recursively)")
    parser.add_argument("--look",    default="warm_film", choices=["warm_film", "teal_orange", "faded"])
    parser.add_argument("--grain",   default=18, type=int)
    parser.add_argument("--workers", default=2, type=int, help="Max concurrent FFmpeg jobs (default 2)")
    args = parser.parse_args()

    source_dir = Path(args.folder).expanduser().resolve()
    if not source_dir.is_dir():
        print(f"{RED}Not a directory: {source_dir}{RESET}")
        sys.exit(1)

    out_dir = source_dir / "filmify"
    out_dir.mkdir(exist_ok=True)

    clips = find_clips(source_dir)
    # Don't re-process files already in the filmify subfolder
    clips = [c for c in clips if c.parent != out_dir]

    if not clips:
        print("No video files found.")
        return

    print(f"\n{BOLD}Filmify batch — {len(clips)} clips → {out_dir}{RESET}")
    print(f"  Look: {args.look}  |  Grain: {args.grain}  |  Workers: {args.workers}\n")

    options = JobOptions(
        look=args.look,
        grain_intensity=args.grain,
        target_fps=24,
        denoise_audio=True,
    )

    sem = asyncio.Semaphore(args.workers)
    tasks = [
        process_one(clip, out_dir, options, sem, i + 1, len(clips))
        for i, clip in enumerate(clips)
    ]
    results = await asyncio.gather(*tasks)

    done  = sum(1 for r in results if r)
    failed = len(results) - done
    print(f"\n{BOLD}Finished: {done} done, {failed} failed.{RESET}")
    print(f"Output folder: {out_dir}\n")


if __name__ == "__main__":
    asyncio.run(main())

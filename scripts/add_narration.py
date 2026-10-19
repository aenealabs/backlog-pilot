"""Merge a recorded voice-over into the demo video.

Keeps the video stream untouched, levels the voice to a standard loudness, lets you shift it
earlier or later, and embeds the captions (the .vtt next to the video) as a subtitle track.

    python scripts/add_narration.py --video demo.mp4 --voice voice.m4a --out narrated.mp4
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--voice", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--offset", type=float, default=0.0,
                        help="seconds to shift the voice: positive starts it later")
    args = parser.parse_args()
    video, voice, out = args.video.expanduser(), args.voice.expanduser(), args.out.expanduser()
    captions = video.with_suffix(".vtt")

    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(video)]
    if args.offset >= 0:
        cmd += ["-itsoffset", str(args.offset), "-i", str(voice)]
    else:
        cmd += ["-ss", str(-args.offset), "-i", str(voice)]
    maps = ["-map", "0:v", "-map", "[voice]"]
    if captions.exists():
        cmd += ["-i", str(captions)]
        maps += ["-map", "2:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng"]
    cmd += ["-filter_complex", "[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,apad[voice]", *maps,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
            "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Render a local launch film, prepare measured music cues, mix and check its MP4.

Generated pages and their approved assets belong in one public film folder. The browser
cannot read outside it or fetch remote content. Provider keys never belong in that folder.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from studio_contracts import MAX_LAUNCH_VIDEO_BYTES, validate_launch_video

HERE = Path(__file__).resolve().parent
FF = os.environ.get("FFMPEG", "ffmpeg")
PROBE = os.environ.get("FFPROBE", "ffprobe")
DIMENSIONS = {"landscape": (1920, 1080), "portrait": (1080, 1920), "square": (1080, 1080)}


def probe(path):
    return json.loads(
        subprocess.check_output(
            [PROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            timeout=30,
        )
    )


def seconds(value):
    number = float(value)
    if not math.isfinite(number) or not 8 <= number <= 90:
        raise argparse.ArgumentTypeError("duration must be 8–90 seconds")
    return number


def analyse(args):
    import numpy as np
    from PIL import Image, ImageDraw

    if not math.isfinite(args.start) or args.start < 0:
        raise ValueError("music start must be finite and nonnegative")
    args.outdir.mkdir(parents=True, exist_ok=True)
    cut = args.outdir / "music.wav"
    subprocess.run(
        [
            FF,
            "-y",
            "-v",
            "error",
            "-ss",
            str(args.start),
            "-i",
            str(args.music),
            "-t",
            str(args.duration),
            "-af",
            f"afade=t=out:st={args.duration - 0.15}:d=0.15",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(cut),
        ],
        check=True,
        timeout=120,
    )
    pcm = subprocess.check_output(
        [
            FF,
            "-v",
            "error",
            "-i",
            str(cut),
            "-t",
            str(args.duration),
            "-ar",
            "8000",
            "-ac",
            "1",
            "-f",
            "f32le",
            "-",
        ],
        timeout=120,
    )
    audio = np.frombuffer(pcm, dtype="<f4")
    if abs(len(audio) / 8000 - args.duration) > 0.04:
        raise ValueError("selected music ends before the film; choose a longer cut")
    windows = audio[: len(audio) // 160 * 160].reshape(-1, 160)
    rms = np.sqrt(np.mean(windows**2, axis=1))
    if not len(rms) or not np.isfinite(rms).all() or float(rms.max()) < 0.00001:
        raise ValueError("music cut has no usable sound")
    energy = rms / rms.max()
    rises = np.maximum(0, np.diff(energy, prepend=energy[0]))
    threshold = max(0.04, float(np.percentile(rises, 80)))
    onsets = []
    for i in range(1, len(rises) - 1):
        if rises[i] >= threshold and rises[i] >= rises[i - 1] and rises[i] >= rises[i + 1]:
            t = round(i * 0.02, 3)
            if not onsets or t - onsets[-1] >= 0.15:
                onsets.append(t)
    cues = {
        "duration": args.duration,
        "source_start": args.start,
        "window_seconds": 0.02,
        "rms": energy.round(5).tolist(),
        "onset_times": onsets,
        "limits": "Measured energy rises are not a verified beat grid or listening review.",
    }
    (args.outdir / "cues.json").write_text(json.dumps(cues), encoding="utf-8")
    image = Image.new("RGB", (1200, 240), "#111111")
    draw = ImageDraw.Draw(image)
    for i, value in enumerate(energy):
        x = round(i * 1199 / max(1, len(energy) - 1))
        height = round(float(value) * 100)
        draw.line((x, 120 - height, x, 120 + height), fill="#8cc3b4")
    for t in onsets:
        x = round(t / args.duration * 1199)
        draw.line((x, 0, x, 239), fill="#f1bb64")
    image.save(args.outdir / "waveform.png")
    print(
        json.dumps({"music": str(cut), "duration": args.duration, "measured_onsets": len(onsets)})
    )


def mix(args):
    duration = float(probe(args.video)["format"]["duration"])
    seconds(duration)
    tracks = [p for p in (args.music, args.effects, args.voice) if p is not None]
    if args.silent and tracks or not args.silent and args.music is None:
        raise ValueError("choose music, or explicit --silent without other sound")
    if args.voice and float(probe(args.voice)["format"]["duration"]) > duration + 0.05:
        raise ValueError("narration exceeds the agreed film length; edit it before mixing")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="launch-mix-") as temp:
        prepared = Path(temp) / "mix.wav"
        command = [FF, "-y", "-v", "error"]
        if args.silent:
            command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            graph = f"[0:a]atrim=duration={duration}[mix]"
        else:
            for track in tracks:
                command += ["-i", str(track)]
            parts = [
                f"[{i}:a]apad,atrim=duration={duration},volume={0.25 if args.voice and i == 0 else 1}[a{i}]"
                for i in range(len(tracks))
            ]
            parts += [
                "".join(f"[a{i}]" for i in range(len(tracks)))
                + f"amix=inputs={len(tracks)}:normalize=0[mix]"
            ]
            graph = ";".join(parts)
        subprocess.run(
            command
            + [
                "-filter_complex",
                graph,
                "-map",
                "[mix]",
                "-ar",
                "48000",
                "-ac",
                "2",
                str(prepared),
            ],
            check=True,
            timeout=120,
        )
        if args.silent:
            subprocess.run(
                [
                    FF,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(args.video),
                    "-i",
                    str(prepared),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-t",
                    str(duration),
                    "-movflags",
                    "+faststart",
                    str(args.out),
                ],
                check=True,
                timeout=120,
            )
        else:
            subprocess.run(
                [
                    "bash",
                    str(HERE / "launch/mix.sh"),
                    str(args.video),
                    str(prepared),
                    str(args.out),
                ],
                check=True,
                timeout=180,
            )
    print(json.dumps({"path": str(args.out), "duration": duration, "silent": args.silent}))


def check_picture(path, duration):
    seconds(duration)
    frame_bytes = 64 * 36 * 3
    sample_limit = math.ceil(duration * 2)
    raw = subprocess.check_output(
        [
            FF,
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-t",
            str(duration),
            "-vf",
            "fps=2,scale=64:36",
            "-frames:v",
            str(sample_limit),
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        timeout=120,
    )
    if not raw or len(raw) % frame_bytes or len(raw) > sample_limit * frame_bytes:
        raise ValueError("decoded picture samples are missing or invalid")
    for offset in range(0, len(raw), frame_bytes):
        frame = raw[offset : offset + frame_bytes]
        # Allow codec rounding; reject only films whose every sampled frame is flat.
        if any(max(frame[channel::3]) - min(frame[channel::3]) > 2 for channel in range(3)):
            return len(raw) // frame_bytes
    raise ValueError("launch video contains only uniform blank picture samples")


def check(args):
    if args.path.stat().st_size > MAX_LAUNCH_VIDEO_BYTES:
        raise ValueError("launch MP4 exceeds the 16 MB output limit")
    result = validate_launch_video(args.path.read_bytes())
    if (result.width, result.height) != DIMENSIONS[args.format]:
        raise ValueError("rendered shape differs from the agreed film format")
    if abs(result.duration_seconds - args.duration) > 0.05:
        raise ValueError("rendered length differs from the agreed film duration")
    info = probe(args.path)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    audios = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if (
        len(videos) != 1
        or len(audios) != 1
        or videos[0]["codec_name"] != "h264"
        or audios[0]["codec_name"] != "aac"
    ):
        raise ValueError("delivery requires one H.264 video and one AAC audio stream")
    if (videos[0].get("width"), videos[0].get("height")) != DIMENSIONS[args.format]:
        raise ValueError("encoded picture differs from the agreed film format")
    for name, stream in (("video", videos[0]), ("audio", audios[0])):
        try:
            duration = float(stream.get("duration", "nan"))
        except (TypeError, ValueError):
            duration = math.nan
        if not math.isfinite(duration) or abs(duration - args.duration) > 0.05:
            raise ValueError(f"{name} stream differs from the agreed film duration")
    subprocess.run(
        [
            FF,
            "-v",
            "error",
            "-xerror",
            "-i",
            str(args.path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        check=True,
        timeout=180,
    )
    picture_samples = check_picture(args.path, args.duration)
    print(
        json.dumps(
            {
                "path": str(args.path),
                "width": result.width,
                "height": result.height,
                "duration": result.duration_seconds,
                "full_decode": "passed",
                "nonuniform_picture": "sampled",
                "picture_samples": picture_samples,
            }
        )
    )


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"render", "stills", "review"}:
        mode = argv.pop(0)
        script = "review_frames.mjs" if mode == "review" else "render.mjs"
        prefix = [] if mode == "review" else ["video" if mode == "render" else "stills"]
        return subprocess.call(["node", str(HERE / "launch" / script), *prefix, *argv])
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    audio = commands.add_parser("analyse")
    audio.add_argument("--music", type=Path, required=True)
    audio.add_argument("--start", type=float, default=0)
    audio.add_argument("--duration", type=seconds, required=True)
    audio.add_argument("--outdir", type=Path, required=True)
    mixer = commands.add_parser("mix")
    mixer.add_argument("--video", type=Path, required=True)
    for name in ("music", "effects", "voice"):
        mixer.add_argument("--" + name, type=Path)
    mixer.add_argument("--silent", action="store_true")
    mixer.add_argument("--out", type=Path, required=True)
    delivery = commands.add_parser("check")
    delivery.add_argument("path", type=Path)
    delivery.add_argument("--format", choices=DIMENSIONS, required=True)
    delivery.add_argument("--duration", type=seconds, required=True)
    args = parser.parse_args(argv)
    try:
        {"analyse": analyse, "mix": mix, "check": check}[args.command](args)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Launch video failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

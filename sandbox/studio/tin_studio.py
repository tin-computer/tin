#!/usr/bin/env python3
"""tin-studio: the creative-studio toolkit inside a Tin sandbox.

  tin-studio inspect <url> <workdir>                 page outline, selectors, colors, screenshots
  tin-studio capture <script.json> <workdir>         keyframes + log.json (no real-time recording)
  tin-studio voice <workdir> [--voice Kore] [--style TEXT] [--language "English (US)"]
  tin-studio render <workdir> <out.mp4> [--character path.svg] [--bg mesh] [--fps 30]
  tin-studio frame <workdir> <out.png> --at SECONDS [--character path.svg] [--bg mesh]
  tin-studio rasterize <character.svg> <out.png> [--mouth mouth-open] [--eyes eyes-closed]
                                                 [--expression expr-happy] [--width 1024]
  tin-studio check <artifact.svg|artifact.mp4>       validate against the Tin output contract
  tin-studio probe <file.mp4|file.mp3>               ffprobe summary

Work in a scratch directory outside the project checkout; only the declared artifact belongs in
the checkout. Voice lines cost money and are rate-limited per run: keep them short and few.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PYTHON = sys.executable
RESVG = os.environ.get("TIN_STUDIO_RESVG") or shutil.which("resvg") or os.path.join(HERE, "resvg")


def run(script, args):
    return subprocess.call([PYTHON, os.path.join(HERE, script), *args])


def rasterize(args):
    from studio_contracts import character_variant_svg

    if len(args) < 2:
        raise SystemExit(
            "usage: tin-studio rasterize <character.svg> <out.png> [--mouth ID] [--eyes ID] [--expression ID] [--width N]"
        )
    opt = lambda name, default=None: args[args.index(name) + 1] if name in args else default  # noqa: E731
    content = open(args[0], "rb").read()
    variant = character_variant_svg(
        content,
        mouth=opt("--mouth", "mouth-closed"),
        eyes=opt("--eyes", "eyes-open"),
        expression=opt("--expression"),
    )
    tmp = args[1] + ".variant.svg"
    open(tmp, "wb").write(variant)
    try:
        subprocess.run([RESVG, "--width", opt("--width", "1024"), tmp, args[1]], check=True)
    finally:
        os.remove(tmp)
    print(f"wrote {args[1]}")
    return 0


def check(args):
    from studio_contracts import validate_character_svg, validate_demo_video

    if len(args) != 1:
        raise SystemExit("usage: tin-studio check <artifact.svg|artifact.mp4>")
    path = args[0]
    content = open(path, "rb").read()
    try:
        if path.lower().endswith(".svg"):
            result = validate_character_svg(content)
        elif path.lower().endswith(".mp4"):
            result = validate_demo_video(content)
        else:
            raise SystemExit("check accepts a .svg character or a .mp4 demo")
    except ValueError as exc:
        print(f"REJECTED: {exc}")
        return 1
    print(f"OK: {result}")
    return 0


def probe(args):
    if len(args) != 1:
        raise SystemExit("usage: tin-studio probe <file>")
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            args[0],
        ]
    )
    print(json.dumps(json.loads(out), indent=1))
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    command, args = sys.argv[1], sys.argv[2:]
    if command == "inspect":
        if len(args) != 2:
            raise SystemExit("usage: tin-studio inspect <url> <workdir>")
        return run("capture.py", ["--inspect", *args])
    if command == "capture":
        return run("capture.py", args)
    if command == "voice":
        return run("voice.py", args)
    if command == "render":
        return run("render.py", args)
    if command == "frame":
        if "--at" not in args or len(args) < 3:
            raise SystemExit("usage: tin-studio frame <workdir> <out.png> --at SECONDS [...]")
        at = args[args.index("--at") + 1]
        rest = [a for i, a in enumerate(args) if a != "--at" and (i == 0 or args[i - 1] != "--at")]
        return run("render.py", [rest[0], rest[1], "--frame-at", at, *rest[2:]])
    if command == "rasterize":
        return rasterize(args)
    if command == "check":
        return check(args)
    if command == "probe":
        return probe(args)
    raise SystemExit(f"unknown command {command!r}\n{__doc__}")


if __name__ == "__main__":
    raise SystemExit(main())

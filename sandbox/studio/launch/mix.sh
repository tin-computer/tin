#!/usr/bin/env bash
# Put the cut music (and an optional SFX track) under the silent picture at -14 LUFS integrated, <= -1 dBTP.
# Usage: tools/mix.sh <silent.mp4> <music.wav> <out.mp4> [sfx.wav]
#
# Two passes: measure the mix, then apply ONE linear gain (so the music's build-to-drop dynamics survive) and a
# gentle peak limiter (-2.2 dBFS at 4x the sample rate, leaving headroom for the AAC encoder) and verify.
# Fixed in place: the original single-pass dynamic loudnorm missed the target on a 32 s film (-12.7 LUFS,
# -0.2 dBTP) and squeezed the loudness range from 7.6 to 3.2 LU, flattening the drop.
set -euo pipefail
FF="${FFMPEG:-ffmpeg}"
v="$1"; m="$2"; o="$3"; sfx="${4:-}"
TARGET=-14.0
if [[ -n "$sfx" ]]; then
  inputs=(-i "$v" -i "$m" -i "$sfx"); pre="[1:a][2:a]amix=inputs=2:normalize=0:duration=first[a]"
else
  inputs=(-i "$v" -i "$m"); pre="[1:a]anull[a]"
fi
measure() {  # integrated loudness and true peak of the audio of a file or filter graph
  "$FF" -hide_banner -nostats "$@" 2>&1 | awk '/Integrated loudness:/{f=1} f&&/ I:/{i=$2} /True peak:/{p=1} p&&/Peak:/{tp=$2} END{print i, tp}'
}
read -r I _ < <(measure "${inputs[@]}" -filter_complex "$pre;[a]ebur128=peak=true[x]" -map "[x]" -f null -)
gain=$(python3 -c 'import sys; print(round(float(sys.argv[1]) - float(sys.argv[2]), 2))' "$TARGET" "$I")
for pass in 1 2; do
  "$FF" -y -loglevel error "${inputs[@]}" -filter_complex \
    "$pre;[a]volume=${gain}dB,aresample=192000,alimiter=limit=0.78:attack=1:release=60:level=disabled,aresample=48000[aout]" \
    -map 0:v -map "[aout]" -c:v copy -c:a aac -b:a 256k -ar 48000 -shortest -movflags +faststart "$o"
  read -r Io TPo < <(measure -i "$o" -af ebur128=peak=true -f null -)
  off=$(python3 -c 'import sys; print(round(float(sys.argv[1]) - float(sys.argv[2]), 2))' "$TARGET" "$Io")
  if python3 -c 'import sys; sys.exit(0 if abs(float(sys.argv[1])) <= 0.15 else 1)' "$off"; then break; fi
  gain=$(python3 -c 'import sys; print(round(float(sys.argv[1]) + float(sys.argv[2]), 2))' "$gain" "$off")
done
echo "wrote $o  (mix measured $I LUFS, gain ${gain} dB -> $Io LUFS integrated, $TPo dBTP)"

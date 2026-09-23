#!/usr/bin/env sh
# Extract JPEG frames of the Y-B-Class fall clips into the instrumentation-test
# assets. The frames are third-party footage, so they are generated locally and
# never committed (see .gitignore); the on-device test skips when they are absent.
#
# Usage: scripts/prepare-instrumentation-frames.sh [clips-dir]
#   clips-dir defaults to the sibling mantau-AI checkout's data/falls
#   (video_1.mp4 = fall, video_5.mp4 = walking only).
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
clips=${1:-"$here/../../mantau-AI/data/falls"}
out="$here/app/src/androidTest/assets/frames"
for pair in video1:video_1.mp4 video5:video_5.mp4; do
    name=${pair%%:*}
    clip="$clips/${pair#*:}"
    rm -rf "${out:?}/$name"
    mkdir -p "$out/$name"
    ffmpeg -loglevel error -i "$clip" -q:v 3 "$out/$name/%04d.jpg"
    ffprobe -v error -select_streams v -show_entries stream=r_frame_rate -of csv=p=0 "$clip"         > "$out/$name/fps.txt"
    echo "$name: $(ls "$out/$name" | grep -c jpg) frames"
done

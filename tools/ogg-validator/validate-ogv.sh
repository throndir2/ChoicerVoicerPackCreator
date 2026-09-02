#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: validate-ogv /path/to/video.ogv" >&2
    exit 64
fi

video=$1
if [ ! -f "$video" ]; then
    echo "OGV does not exist: $video" >&2
    exit 66
fi

printf '%s\n' '=== oggz-validate ==='
oggz-validate "$video"

printf '%s\n' '=== libvorbis full decode ==='
oggdec -Q -o /dev/null "$video"

printf '%s\n' '=== libtheora full decode ==='
theora_dump_video "$video" > /dev/null

printf '%s\n' '=== stream info ==='
# Framing and complete reference decodes above are the hard gates. Some FFmpeg
# Theora files produce non-fatal frame-number diagnostics in ogginfo.
ogginfo "$video" || true

printf '%s\n' 'XIPH OGV VALIDATION PASSED'

#!/usr/bin/env python3
"""Assemble a faceless, vertical UGC-style video from voiceover + b-roll.

Pipeline: transcribe the voiceover for burned-in captions -> normalize and
jump-cut the b-roll clips to fill the voiceover's runtime -> mix voiceover
(+ optional ducked music) -> overlay a hook card, captions, and a CTA card.

Usage:
    python3 scripts/make_ugc_video.py \
        --voiceover assets/voiceover.m4a \
        --broll assets/broll1.mov assets/broll2.mov \
        --output out/final.mp4 \
        --hook "This app changed how I edit videos" \
        --cta "Try Epidemic Sound free - link in bio"
"""
import argparse
import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

WIDTH, HEIGHT, FPS = 1080, 1920, 30

# (font file, ASS family name) - first one found on disk wins. Covers the
# common Linux (Debian/Ubuntu) and macOS default locations.
FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu Sans"),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "Arial"),
    ("/Library/Fonts/Arial Bold.ttf", "Arial"),
]


def find_font():
    """Return (fontfile_path_or_None, ass_family_name) for this machine."""
    for path, family in FONT_CANDIDATES:
        if Path(path).exists():
            return path, family
    return None, "Arial"


def run(cmd):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(result.stderr[-4000:])
        raise subprocess.CalledProcessError(result.returncode, cmd)


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def transcribe(voiceover_path, max_words_per_chunk=3):
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(voiceover_path), word_timestamps=True)

    words = []
    for seg in segments:
        for w in seg.words:
            words.append((w.start, w.end, w.word.strip()))

    chunks = []
    for i in range(0, len(words), max_words_per_chunk):
        group = words[i:i + max_words_per_chunk]
        start = group[0][0]
        end = group[-1][1]
        text = " ".join(w[2] for w in group)
        chunks.append((start, end, text))
    return chunks, words


def ass_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(chunks, ass_path, font_family="Arial"):
    # Style matches the reference: bold white text with a soft glow (no hard
    # box/outline), natural mixed case, centered on the frame.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font_family},104,&H00FFFFFF,&H000000FF,&H00303030,&H00000000,1,0,0,0,100,100,0,0,1,3,0,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for start, end, text in chunks:
        text_escaped = text.replace("\n", " ")
        lines.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{{\\blur4}}{text_escaped}\n"
        )
    ass_path.write_text("".join(lines))


def normalize_clip(src, dst):
    vf = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},fps={FPS}"
    )
    run([
        "ffmpeg", "-y", "-i", str(src),
        "-vf", vf, "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(dst),
    ])


def assemble_broll(broll_paths, total_duration, cut_length, tmpdir):
    tmpdir = Path(tmpdir)
    normalized = []
    for i, p in enumerate(broll_paths):
        norm = tmpdir / f"norm_{i}.mp4"
        normalize_clip(p, norm)
        normalized.append((norm, probe_duration(norm)))

    segments = []
    idx = 0
    cursor = {i: 0.0 for i in range(len(normalized))}
    remaining = total_duration
    seg_i = 0
    while remaining > 0:
        clip_path, clip_dur = normalized[idx % len(normalized)]
        start = cursor[idx % len(normalized)]
        if start >= clip_dur:
            start = 0.0
        length = min(cut_length, clip_dur - start, remaining)
        if length <= 0.05:
            idx += 1
            continue
        seg_path = tmpdir / f"seg_{seg_i}.mp4"
        run([
            "ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(clip_path),
            "-t", f"{length:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            str(seg_path),
        ])
        segments.append(seg_path)
        cursor[idx % len(normalized)] = start + length
        remaining -= length
        idx += 1
        seg_i += 1

    list_path = tmpdir / "concat_list.txt"
    list_path.write_text("".join(f"file '{s.resolve()}'\n" for s in segments))
    broll_out = tmpdir / "broll_assembled.mp4"
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy", str(broll_out),
    ])
    return broll_out


def build_final(broll_video, voiceover, music, ass_path, hook_text, cta_text,
                 total_duration, output, font_path=None, font_family="Arial"):
    inputs = ["-i", str(broll_video), "-i", str(voiceover)]
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]

    hook_dur = 1.8
    cta_dur = 2.5
    cta_start = max(0.0, total_duration - cta_dur)

    def esc(t):
        return t.replace(":", "\\:").replace("'", "’")

    def wrap_lines(text, fontsize, char_width_ratio=0.56):
        max_chars = max(10, int(WIDTH * 0.86 / (fontsize * char_width_ratio)))
        return textwrap.wrap(text, max_chars) or [text]

    # Use an actual font file when we found one on disk; otherwise fall back
    # to a fontconfig family-name lookup so this still works cross-platform.
    font_clause = f"fontfile='{font_path}'" if font_path else f"font='{font_family}'"

    def text_block(text, fontsize, y_center_expr, enable_expr):
        # Stack one drawtext filter per line rather than embedding a newline
        # in a single drawtext's text value: ffmpeg's own escape sequence for
        # a line break inside a quoted -vf value is unreliable across builds
        # (observed literally rendering "n" instead of breaking on ffmpeg 9),
        # so multiple filters sidesteps that escaping layer entirely.
        lines = wrap_lines(text, fontsize)
        line_height = fontsize * 1.3
        top_offset = -(len(lines) - 1) * line_height / 2
        clauses = []
        for i, line in enumerate(lines):
            y = f"({y_center_expr})+({top_offset + i * line_height:.1f})"
            clauses.append(
                f"drawtext=text='{esc(line)}':{font_clause}"
                f":fontcolor=white:fontsize={fontsize}:shadowcolor=black@0.85:shadowx=3:shadowy=3"
                f":x=(w-text_w)/2:y={y}:enable='{enable_expr}'"
            )
        return ",".join(clauses)

    # No boxed background, to match the reference's glowing-text-over-footage
    # look; a dark drop shadow keeps it legible against bright b-roll instead.
    drawtext_hook = text_block(hook_text, 64, "h*0.30", f"between(t,0,{hook_dur})")
    drawtext_cta = text_block(
        cta_text, 50, "h*0.82",
        f"between(t,{cta_start:.2f},{total_duration:.2f})",
    )

    vf = f"{drawtext_hook},{drawtext_cta},subtitles={ass_path}"

    if music:
        filter_complex = (
            f"[1:a]volume=1.0[vo];"
            f"[2:a]volume=0.12[mus];"
            f"[vo][mus]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        cmd = [
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-vf", vf,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(output),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", *inputs,
            "-vf", vf,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "medium", "-crf", "19",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(output),
        ]
    run(cmd)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voiceover", required=True, type=Path)
    ap.add_argument("--broll", required=True, type=Path, nargs="+")
    ap.add_argument("--music", type=Path, default=None)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--hook", default=None, help="Hook text card shown for the first ~1.8s")
    ap.add_argument("--cta", default="Try Epidemic Sound free - link in bio")
    ap.add_argument("--cut-length", type=float, default=2.5, help="Seconds per b-roll jump cut")
    ap.add_argument("--words-per-caption", type=int, default=2)
    args = ap.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(args.voiceover)
    print(f"Voiceover duration: {duration:.2f}s")

    print("Transcribing voiceover for captions...")
    chunks, words = transcribe(args.voiceover, args.words_per_caption)
    full_text = " ".join(w[2] for w in words)
    print(f"Transcript: {full_text}")

    default_hook = " ".join(w[2] for w in words[:6]) if words else ""
    hook_text = args.hook or default_hook

    font_path, font_family = find_font()
    print(f"Using font: {font_path or font_family}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ass_path = tmp / "captions.ass"
        build_ass(chunks, ass_path, font_family)

        print("Assembling b-roll with jump cuts...")
        broll_video = assemble_broll(args.broll, duration, args.cut_length, tmp)

        print("Compositing final video...")
        build_final(broll_video, args.voiceover, args.music, ass_path,
                     hook_text, args.cta, duration, args.output,
                     font_path, font_family)

    print(f"Done: {args.output}")


if __name__ == "__main__":
    main()

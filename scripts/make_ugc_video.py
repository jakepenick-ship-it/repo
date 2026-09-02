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


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


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


def build_ass(chunks, ass_path):
    # Style matches the reference: bold white text with a soft glow (no hard
    # box/outline), natural mixed case, centered on the frame.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,DejaVu Sans,104,&H00FFFFFF,&H000000FF,&H00303030,&H00000000,1,0,0,0,100,100,0,0,1,3,0,5,80,80,0,1

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
                 total_duration, output):
    inputs = ["-i", str(broll_video), "-i", str(voiceover)]
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]

    hook_dur = 1.8
    cta_dur = 2.5
    cta_start = max(0.0, total_duration - cta_dur)

    def esc(t):
        return t.replace(":", "\\:").replace("'", "’")

    def wrap(text, fontsize, char_width_ratio=0.56):
        max_chars = max(10, int(WIDTH * 0.86 / (fontsize * char_width_ratio)))
        return "\n".join(textwrap.wrap(text, max_chars)) or text

    hook_fontsize, cta_fontsize = 64, 50
    hook_wrapped = wrap(hook_text, hook_fontsize)
    cta_wrapped = wrap(cta_text, cta_fontsize)

    # No boxed background, to match the reference's glowing-text-over-footage
    # look; a dark drop shadow keeps it legible against bright b-roll instead.
    drawtext_hook = (
        f"drawtext=text='{esc(hook_wrapped)}':fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        f":fontcolor=white:fontsize={hook_fontsize}:shadowcolor=black@0.85:shadowx=3:shadowy=3"
        f":x=(w-text_w)/2:y=(h*0.30):line_spacing=10"
        f":enable='between(t,0,{hook_dur})'"
    )
    drawtext_cta = (
        f"drawtext=text='{esc(cta_wrapped)}':fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        f":fontcolor=white:fontsize={cta_fontsize}:shadowcolor=black@0.85:shadowx=3:shadowy=3"
        f":x=(w-text_w)/2:y=(h*0.82):line_spacing=8"
        f":enable='between(t,{cta_start:.2f},{total_duration:.2f})'"
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

    hook_text = args.hook or (words[0][2] if not words else " ".join(w[2] for w in words[:6]))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ass_path = tmp / "captions.ass"
        build_ass(chunks, ass_path)

        print("Assembling b-roll with jump cuts...")
        broll_video = assemble_broll(args.broll, duration, args.cut_length, tmp)

        print("Compositing final video...")
        build_final(broll_video, args.voiceover, args.music, ass_path,
                     hook_text, args.cta, duration, args.output)

    print(f"Done: {args.output}")


if __name__ == "__main__":
    main()

# UGC Video Pipeline

Assembles a faceless, vertical (9:16) UGC-style ad from a voiceover recording
and b-roll clips: auto-transcribed burned-in captions, jump-cut b-roll,
a hook card in the first ~1.8s, and a CTA end card.

## Setup

```
pip install -r requirements.txt
# ffmpeg must be on PATH
```

## Assets

`assets/voiceover.m4a` is already in this repo (pulled from Drive).

The b-roll (`F2412353-F575-4A2A-87A8-D769693F029F.mov`, ~46MB) and the style
reference (`CF17A668-40CB-4CFE-9E76-07631EA5365F.mov`, ~32MB) are too large
for this sandbox's Drive connector (10MB cap) — download them yourself from
Drive and drop them in `assets/` before running the script locally.

## Usage

```
python3 scripts/make_ugc_video.py \
    --voiceover assets/voiceover.m4a \
    --broll assets/broll1.mov assets/broll2.mov \
    --output out/final.mp4 \
    --hook "This app changed how I edit videos" \
    --cta "Try Epidemic Sound free - link in bio"
```

Options:
- `--broll` accepts multiple files; the script cycles through them with
  jump cuts (`--cut-length`, default 2.5s) to fill the voiceover's runtime.
- `--music` optionally mixes in a background track, ducked under the
  voiceover.
- `--hook` defaults to the first few transcribed words if omitted.
- `--words-per-caption` controls how many words appear per caption burst
  (default 3).

Output is 1080x1920 H.264 / AAC, matched to the voiceover's exact duration.

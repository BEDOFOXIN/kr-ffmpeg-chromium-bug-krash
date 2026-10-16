# ffmpeg WebM muxer writes a negative `DiscardPadding` that crashes Chromium/Discord

## Summary

Some WebM files decode cleanly in ffmpeg (zero errors) but crash Chromium-based
players such as the Discord desktop client. The trigger is a combination of two
audio-track anomalies:

1. a positive `CodecDelay` on the audio `TrackEntry` (`2 666 667 ns` = 128 samples
   @48 kHz), written by ffmpeg 7.1;
2. one or more audio blocks **in the middle** of the stream wrapped in a
   `BlockGroup` with a **negative** `DiscardPadding` (`−21 354 167 ns` = −1025
   samples @48 kHz). Per the Matroska spec, `DiscardPadding` is unsigned, so a
   negative value is invalid.

Both conditions are required (see the bisection matrix below). The bug is
content-independent and resolution-independent: it reproduces with a sine tone at
320×240 and with pink noise at 720×1280.

## Environment

| Component | Version |
|-----------|---------|
| ffmpeg (writer) | 7.1, `Lavf61.7.100` |
| ffmpeg (reader/re-mux, non-crashing) | 8.x, `Lavf62.3.100` |
| Codec | VP8 + Vorbis (Matroska/WebM) |
| Affected player | Chromium / Discord desktop client |

## Steps to reproduce

Minimal reproduction uses three dependency-free Python scripts:
`ebml_break.py`, `mkv_add_codecdelay.py`, `mkv_inject_crash.py`.

```bash
# 1. Clean WebM (content is irrelevant; any VP8/Vorbis file works).
ffmpeg -y -f lavfi -i "testsrc2=size=320x240:rate=30" \
       -f lavfi -i "sine=frequency=440:sample_rate=48000" \
       -t 6 -c:v libvpx -b:v 500k -c:a libvorbis clean.webm

# 2. Add CodecDelay to the audio track (what ffmpeg 7.1 wrote).
python mkv_add_codecdelay.py clean.webm step2.webm

# 3. Wrap 3 mid-stream audio blocks into BlockGroups with a negative
#    DiscardPadding (-1025 samples).
python mkv_inject_crash.py step2.webm crash.webm --start-index 200 --count 3
```

### Result

- `ffmpeg -v error -i crash.webm -f null -` → **no errors**.
- Opening `crash.webm` in Chromium / Discord → **crash**.

## Root cause

Two elements combine:

| # | Element | Location | Bytes / value |
|---|---------|----------|---------------|
| 1 | `CodecDelay` | `Tracks` → `TrackEntry` (type 2) | `56 aa 83 28 b0 ab` = 2 666 667 ns (128 samples) |
| 2 | `DiscardPadding` < 0 | `Cluster` → `BlockGroup` → `DiscardPadding` (0x75A2) | −21 354 167 ns (−1025 samples) or −20 833 ns (−1 sample) |

Additionally, the `BlockGroup` must not be the last audio block in the stream:
a negative `DiscardPadding` stretches the block backward so that it overlaps the
following block. With no following block there is nothing to overlap and no crash.

ffmpeg itself tolerates these values (it just uses them arithmetically), which is
why the corruption is invisible to ffmpeg but fatal to Chromium. The exact internal
Chromium failure mode is not confirmed; the crash itself is reproducible.

### Version behavior

| Writer | Behavior |
|--------|----------|
| `Lavf61.7.100` (ffmpeg 7.1) | writes `BlockGroup` + negative `DiscardPadding` + `CodecDelay` |
| `Lavf62.3.100` (ffmpeg 8.x) | re-muxes the blocks as plain `SimpleBlock` without `DiscardPadding` |

## Bisection matrix

| Change from the original crashing file | Crash |
|-----------------------------------------|-------|
| original (`cat.webm`, ffmpeg 7.1) | yes |
| restore only `SeekHead`/`Cues` | yes → index irrelevant |
| replace only Info/Tracks/Tags | yes → metadata irrelevant |
| set block flag 0x00→0x80 only | yes → flag irrelevant |
| `BlockGroup` without `DiscardPadding` | no |
| `BlockGroup` → `SimpleBlock`, drop `DiscardPadding` | no → `DiscardPadding` is the trigger |
| no `CodecDelay` + negative `DiscardPadding` | no |
| `CodecDelay` + no `DiscardPadding` | no |
| `CodecDelay` + negative `DiscardPadding` | **yes → both are required** |

## Suggested fix

In the Matroska muxer/demuxer (`libavformat/matroska*.c`):

1. **Muxer**: clamp `DiscardPadding` to non-negative values before writing.
   `MATROSKA_ID_DISCARDPADDING` is unsigned per spec, so a negative value must
   never reach the output.
2. **Demuxer**: when a negative `DiscardPadding` is encountered, ignore it and
   emit a warning (currently it is silently passed through).
3. **Investigate the 7.1 pipeline**: determine why negative `DiscardPadding`
   (−1025 / −1 samples) was produced when `CodecDelay = +128` samples was present
   (likely delay subtraction from trailing block durations).

The minimal change that prevents the crash for already-existing files is item 2:
the demuxer must not expose a negative `DiscardPadding` to the player.

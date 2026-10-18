# Chromium/Discord crashes on WebM with negative `DiscardPadding`

## Summary

Some WebM files decode cleanly in ffmpeg (zero errors) but crash Chromium-based
players such as the Discord desktop client. The trigger is a **valid** Matroska
construct that Chromium mishandles:

- an audio `TrackEntry` carries a `CodecDelay` (`2 666 667 ns` = 128 samples
  @48 kHz);
- one or more audio blocks in the middle of the stream are wrapped in a
  `BlockGroup` with a **negative** `DiscardPadding` (`−21 354 167 ns` = −1025
  samples @48 kHz).

`DiscardPadding` is a **signed** integer per the Matroska spec
([ebml_matroska.xml](https://github.com/ietf-wg-cellar/matroska-specification/blob/1a943ead9bb4c67b02b966cebafe51c8c074e703/ebml_matroska.xml) →
`DiscardPadding`, `type="integer"`):

> Duration of the silent data added to the `Block`, expressed in Matroska Ticks
> -- i.e., in nanoseconds. Padding at the end of the `Block` for positive values
> and at the beginning of the `Block` for negative values.

So negative values are valid and expected; they mean "discard this many samples
from the beginning of the block". Chromium crashes on them instead of honoring
them. **This is a Chromium bug, not an ffmpeg bug** (confirmed by ffmpeg maintainer
James Almer — see <https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/25132>).

## Environment

| Component | Version |
|-----------|---------|
| Chromium | 155.0.8059.82 (also Discord desktop client, Chromium-based) |
| OS | Windows 10/11 |
| Codec | VP8 + Vorbis (Matroska/WebM) |
| Container | WebM (DocType `webm`) |

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

# 3. Wrap 3 mid-stream, full-size (1024-sample) audio blocks into BlockGroups
#    with a negative DiscardPadding (-1025 samples).
python mkv_inject_crash.py step2.webm crash_129.webm --start-index 200 --count 3 --min-step-ms 15
```

### Result

- `ffmpeg -v error -i crash_129.webm -f null -` → **no errors**.
- Opening `crash_129.webm` in Chromium / Discord → **crash**.

## Root cause

Chromium fails to handle a negative `DiscardPadding` in an audio `BlockGroup`.

The crash requires **all** of the following:

| # | Condition | Value / meaning |
|---|-----------|-----------------|
| 1 | Audio codec | **Vorbis** (Opus is not affected) |
| 2 | `CodecDelay` on the audio `TrackEntry` | +2 666 667 ns = +128 samples |
| 3 | `DiscardPadding` negative | −21 354 167 ns = −1025 samples |
| 4 | Wrapped in `BlockGroup` | `BlockGroup` → `Block` + `DiscardPadding` |
| 5 | Mid-stream | another audio block follows |
| 6 | Full-size block | 1024 samples (~21 ms), not a 128-sample micro-block |

Removing any one condition stops the crash (see the bisection matrix below).

Likely failure mode (unconfirmed internals): Chromium subtracts `DiscardPadding`
from the block duration / timestamp and produces a non-monotonic or over/underflow
value instead of trimming samples from the start of the block.

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
| `CodecDelay` + negative `DiscardPadding` (Vorbis) | **yes** |
| same injection on Opus | no → Vorbis-specific |
| negative `DiscardPadding` on 128-sample micro-block | no → full-size block required |
| negative `DiscardPadding` on the last audio block | no → mid-stream required |

## Suggested Chromium-side fix

Honor negative `DiscardPadding` per the Matroska spec: treat it as "discard this
many samples from the beginning of the block", and never let it propagate into
duration/timestamp arithmetic as a plain negative number. At minimum, guard
against non-monotonic/underflow results.

## References

- ffmpeg issue (closed as not-a-bug by maintainer James Almer, who confirmed
  `DiscardPadding` is signed): <https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/25132>
- Matroska spec: `DiscardPadding` = `type="integer"`:
  <https://github.com/ietf-wg-cellar/matroska-specification/blob/1a943ead9bb4c67b02b966cebafe51c8c074e703/ebml_matroska.xml>

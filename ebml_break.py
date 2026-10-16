#!/usr/bin/env python3
"""Reproduce the ``cat.webm`` container corruption pattern.

The broken file (see cat_webm_analysis.md) is a normal seekable WebM whose
``SeekHead`` and ``Cues`` elements have been surgically replaced by ``Void``
elements of *exactly the same on-disk size*.  Every other byte (and therefore
every offset, cluster and timecode) is left untouched, so the file still
decodes cleanly but can no longer be seeked.

This script applies that same transformation to any seekable WebM/Matroska
file so the ffmpeg community has a reproducible way to generate the broken
input and test fixes.

Usage:
    python ebml_break.py --analyze  in.webm            # dump EBML structure
    python ebml_break.py in.webm out.webm              # break the file
"""

import sys

# EBML element IDs we care about.
ID_SEEKHEAD = 0x114D9B74
ID_CUES = 0x1C53BB6B
ID_VOID = 0xEC

TARGETS = (ID_SEEKHEAD, ID_CUES)


def read_element_id(data, pos):
    """Return (length, id_value) for an EBML element ID at ``pos``.

    Element-ID length is the number of leading zero bits in the first byte
    plus one; the ID value is the raw big-endian bytes (marker included).
    """
    if pos >= len(data):
        return None, None
    b = data[pos]
    length = 1
    mask = 0x80
    while mask and not (b & mask):
        length += 1
        mask >>= 1
    if pos + length > len(data):
        return None, None
    value = 0
    for i in range(length):
        value = (value << 8) | data[pos + i]
    return length, value


def read_vint_size(data, pos):
    """Return (length, size_value) for an EBML variable-size integer at ``pos``.

    VINT length is the number of leading zero bits plus one; the value uses
    only the 7*length data bits (the leading marker bit is not part of it).
    """
    b = data[pos]
    length = 1
    mask = 0x80
    while mask and not (b & mask):
        length += 1
        mask >>= 1
    if pos + length > len(data):
        return None, None
    value = b & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    return length, value


def encode_vint(value, length):
    """Encode ``value`` as a ``length``-byte EBML VINT.

    The first byte carries the ``length``-bit marker (``length-1`` zero bits
    followed by a single one bit) plus ``8 - length`` high value bits; each
    following byte carries a full 8 bits of the value.
    """
    out = bytearray(length)
    first_data_bits = 8 - length
    out[0] = 1 << first_data_bits                      # marker: 0..01
    out[0] |= (value >> (8 * (length - 1))) & ((1 << first_data_bits) - 1)
    for i in range(1, length):
        shift = 8 * (length - 1 - i)
        out[i] = (value >> shift) & 0xFF
    return bytes(out)


def make_void(total_size):
    """Build a ``Void`` element occupying exactly ``total_size`` bytes."""
    remaining = total_size - 1  # bytes left for the size field + payload
    for length in range(1, 9):
        payload = remaining - length
        if 0 <= payload <= (1 << (7 * length)) - 1:
            return bytes([ID_VOID]) + encode_vint(payload, length) + b"\x00" * payload
    raise ValueError("cannot encode a %d-byte Void element" % total_size)


def walk(data, start, end, depth, max_depth, sink):
    """Recursively parse EBML elements into ``sink`` (a list of dicts)."""
    pos = start
    while pos < end:
        if end - pos < 2:            # not enough room for ID + size
            break
        id_len, elem_id = read_element_id(data, pos)
        if id_len is None:
            break
        size_pos = pos + id_len
        if size_pos >= end:
            break
        size_len, size = read_vint_size(data, size_pos)
        if size_len is None:
            break
        content_off = size_pos + size_len
        total = id_len + size_len + size
        if content_off + size > end:
            break                   # truncated element, stop at this level
        sink.append({
            "id": elem_id,
            "id_len": id_len,
            "id_off": pos,
            "size_len": size_len,
            "size": size,           # content size
            "content_off": content_off,
            "total": total,         # full on-disk size
            "depth": depth,
        })
        if depth < max_depth:
            walk(data, content_off, content_off + size, depth + 1, max_depth, sink)
        pos = content_off + size


def element_name(elem_id):
    names = {
        0x1A45DFA3: "EBML",
        0x18538067: "Segment",
        0x114D9B74: "SeekHead",
        0x1C53BB6B: "Cues",
        0xEC: "Void",
        0x1549A966: "Info",
        0x1654AE6B: "Tracks",
        0x1254C367: "Tags",
        0x1F43B675: "Cluster",
    }
    return names.get(elem_id, "0x%X" % elem_id)


def find_targets(elements):
    """Return SeekHead/Cues elements (unique per valid Matroska file)."""
    found = {}
    for el in elements:
        if el["id"] in TARGETS and el["id"] not in found:
            found[el["id"]] = el
    return found


def analyze(path):
    data = open(path, "rb").read()
    elements = []
    walk(data, 0, len(data), 0, 1, elements)

    # Show the first two levels (top level + Segment children).
    for el in elements:
        if el["depth"] > 1:
            continue
        indent = "  " * el["depth"]
        print("%s%-12s @%-8d size %d  (total %d)" % (
            indent, element_name(el["id"]), el["id_off"], el["size"], el["total"]))

    targets = find_targets(elements)
    print()
    for key, label in ((ID_SEEKHEAD, "SeekHead"), (ID_CUES, "Cues")):
        el = targets.get(key)
        if el:
            print("%s @%d: content=%d size_field=%d total=%d bytes" % (
                label, el["id_off"], el["size"], el["size_len"], el["total"]))
        else:
            print("%s: not found" % label)


def break_file(path, out_path):
    data = bytearray(open(path, "rb").read())
    elements = []
    walk(data, 0, len(data), 0, 1, elements)
    targets = find_targets(elements)

    if not targets:
        print("no SeekHead/Cues found; nothing to do", file=sys.stderr)
        return 1

    for key, label in ((ID_SEEKHEAD, "SeekHead"), (ID_CUES, "Cues")):
        el = targets.get(key)
        if not el:
            print("warning: %s not found" % label, file=sys.stderr)
            continue
        void = make_void(el["total"])
        assert len(void) == el["total"], (label, len(void), el["total"])
        data[el["id_off"]:el["id_off"] + el["total"]] = void
        print("replaced %s @%d (%d bytes) with equal-size Void" % (
            label, el["id_off"], el["total"]))

    open(out_path, "wb").write(data)
    print("wrote %s" % out_path)
    return 0


def main(argv):
    if len(argv) >= 2 and argv[1] == "--analyze":
        analyze(argv[2])
        return 0
    if len(argv) == 3:
        return break_file(argv[1], argv[2])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

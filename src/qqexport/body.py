# -*- coding: utf-8 -*-
"""parse the QQ NT message body (column 40800)."""

CONTENT_TYPES = {
    1: "text",
    2: "picture",
    3: "file",
    4: "video",
    5: "ptt",
    6: "face",
    7: "sticker",
    8: "link",
    9: "location",
    10: "rich_text",
    11: "reply",
    12: "card",
    13: "forward",
    14: "call",
    15: "system",
    16: "market_face",
    17: "ark",
    18: "struct",
}

MAX_FIELD_NUMBER = 100000
MAX_DEPTH = 6


class Field(object):
    __slots__ = ("number", "wire", "value")

    def __init__(self, number, wire, value):
        self.number = number
        self.wire = wire
        self.value = value

    def __repr__(self):
        return "Field(%d, w%d, %r)" % (self.number, self.wire, _short(self.value))


def _short(v):
    s = repr(v)
    return s if len(s) <= 40 else s[:37] + "..."


def _read_varint(buf, off):
    result = 0
    shift = 0
    while True:
        if off >= len(buf):
            raise ValueError("varint truncated")
        b = buf[off]
        off += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, off
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def parse_wire(buf, start=0, end=None, depth=0):
    """Generic protobuf reader returning a list of Field.

    Truncated or malformed input stops the loop and keeps whatever was read so
    far — QQ blobs are often cut mid-record, so this must never raise.
    """
    if end is None:
        end = len(buf)
    fields = []
    off = start
    while off < end:
        try:
            tag, off = _read_varint(buf, off)
        except ValueError:
            break
        number = tag >> 3
        wire = tag & 7
        if number == 0:
            break
        if wire == 0:
            try:
                val, off = _read_varint(buf, off)
            except ValueError:
                break
            fields.append(Field(number, wire, val))
        elif wire == 1:
            val = bytes(buf[off : off + 8])
            if len(val) < 8:
                break
            off += 8
            fields.append(Field(number, wire, val))
        elif wire == 2:
            try:
                ln, off = _read_varint(buf, off)
            except ValueError:
                break
            val = bytes(buf[off : off + ln])
            if len(val) < ln:
                break
            off += ln
            fields.append(Field(number, wire, val))
        elif wire == 5:
            val = bytes(buf[off : off + 4])
            if len(val) < 4:
                break
            off += 4
            fields.append(Field(number, wire, val))
        else:
            break
    return fields


def fields_to_dict(fields, depth=0):
    """Fold a field list into {number: value}, merging repeated names into lists."""
    out = {}
    for f in fields:
        v = f.value
        if f.wire == 2 and depth < MAX_DEPTH:
            v = _maybe_nested(f.value, depth)
        if f.number in out:
            if isinstance(out[f.number], list):
                out[f.number].append(v)
            else:
                out[f.number] = [out[f.number], v]
        else:
            out[f.number] = v
    return out


def _maybe_nested(blob, depth):
    """Heuristic: if it parses cleanly as a submessage, keep both raw and parsed."""
    if not blob or depth >= MAX_DEPTH:
        return blob
    try:
        sub = parse_wire(blob, 0, None, depth + 1)
    except Exception:  # noqa: BLE001
        return blob
    if not sub:
        return blob
    if not all(0 < f.number < MAX_FIELD_NUMBER for f in sub):
        return blob
    return {"__raw__": blob, "__fields__": fields_to_dict(sub, depth + 1)}


def parse_msg_body(blob):
    """Return a list of content segments (dicts) from a 40800 blob."""
    if not blob:
        return []
    try:
        fields = parse_wire(blob)
    except Exception:  # noqa: BLE001
        return []
    segments = []
    for f in fields:
        if f.number != 40800 or f.wire != 2:
            continue
        seg = _segment(f.value)
        if seg:
            segments.append(seg)
    return segments


def _segment(blob):
    try:
        fields = parse_wire(blob)
    except Exception:  # noqa: BLE001
        return None
    d = fields_to_dict(fields, 1)
    if not d:
        return None
    return d


def _plain(value):
    """Flatten a nested value back to bytes/int/str."""
    if isinstance(value, dict) and "__raw__" in value:
        return value["__raw__"]
    return value


def seg_text(seg):
    """Best-effort plain text for a segment (field 45101)."""
    if not isinstance(seg, dict):
        return ""
    for t in _all(seg, 45101):
        raw = _plain(t)
        if isinstance(raw, bytes):
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
        if isinstance(raw, str):
            return raw
        if isinstance(raw, int):
            return str(raw)
    return ""


def seg_type_name(seg):
    if not isinstance(seg, dict):
        return "unknown"
    for ct in _all(seg, 45002):
        if isinstance(ct, int):
            return CONTENT_TYPES.get(ct, "type%d" % ct)
    return "unknown"


def seg_field(seg, number):
    """Every value stored under `number`, raw (never nested)."""
    return [_plain(v) for v in _all(seg, number)]


def _all(d, number):
    v = d.get(number)
    if v is None:
        return []
    if isinstance(v, list):
        return list(v)
    return [v]


def seg_local_path(seg):
    """Field 45812 — the local on-disk path QQ itself uses for this media."""
    for raw in seg_field(seg, 45812):
        if isinstance(raw, bytes):
            try:
                txt = raw.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            if txt and not txt.startswith("http"):
                return txt
    return ""


def seg_filename(seg):
    for raw in seg_field(seg, 45402):
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        if isinstance(raw, str):
            return raw
    return ""


def seg_filesize(seg):
    for raw in seg_field(seg, 45405):
        if isinstance(raw, int):
            return raw
    return 0


def seg_picture_size(seg):
    w = h = 0
    for raw in seg_field(seg, 45411):
        if isinstance(raw, int):
            w = raw
            break
    for raw in seg_field(seg, 45412):
        if isinstance(raw, int):
            h = raw
            break
    return w, h


def body_summary(blob):
    """Human-readable one-liner for a whole message body (diagnostics + TXT)."""
    segs = parse_msg_body(blob)
    if not segs:
        return "[无法解析]"
    parts = []
    for seg in segs:
        kind = seg_type_name(seg)
        if kind == "text":
            parts.append(seg_text(seg))
        elif kind in ("picture", "video"):
            parts.append("[%s]" % ("图片" if kind == "picture" else "视频"))
        elif kind == "file":
            parts.append("[文件:%s]" % (seg_filename(seg) or "未命名"))
        elif kind == "ptt":
            parts.append("[语音]")
        elif kind in ("face", "sticker", "market_face"):
            parts.append("[表情包]")
        elif kind == "forward":
            parts.append("[合并转发]")
        elif kind == "card":
            parts.append("[名片]")
        elif kind == "call":
            parts.append("[通话]")
        elif kind == "reply":
            parts.append("[引用:%s]" % (seg_text(seg) or ""))
        elif kind == "link":
            parts.append("[链接]")
        elif kind == "system":
            parts.append("[系统消息]")
        else:
            parts.append("[%s]" % kind)
    return "".join(p for p in parts if p)


def debug_dump(blob, indent=0):
    """Pretty-print the whole body tree (diagnostics only)."""
    lines = []

    def walk(b, level):
        pad = "  " * level
        for f in parse_wire(b):
            if f.wire == 2:
                v = f.value
                preview = bytes(v[:40])
                lines.append("%s#%d len=%d %r" % (pad, f.number, len(v), preview))
                if 0 < len(v) <= 4096:
                    try:
                        walk(v, level + 1)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                lines.append("%s#%d = %d" % (pad, f.number, f.value))

    try:
        walk(blob, indent)
    except Exception as exc:  # noqa: BLE001
        lines.append("<parse error: %s>" % exc)
    return "\n".join(lines)

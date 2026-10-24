# -*- coding: utf-8 -*-
"""纯 Python 的 Protobuf 解码器 —— NTQQ 消息体（40800 / 40600 / 40801 / 40900）。

用法::

    from qqexport.proto import parse_40800, decode_message, SCHEMA_MSG_CONTENT

    # 解析一条消息的 40800 消息体
    for seg in parse_40800(blob):
        print(seg.seg_type_name(), seg.summarize())

    # 通用解码：返回 dict
    d = decode_message(blob, "MsgContent")
    print(d.get("text"), d.get("content_type"))

本模块**不依赖 google.protobuf**，全部手写 wire format：

===========  ==================================================
wire type    含义
===========  ==================================================
0            varint（int32/int64/uint32/uint64/bool/enum）
1            64 位定长（double/fixed64/sfixed64）
2            length-delimited（string/bytes/message/packed）
5            32 位定长（float/fixed32/sfixed32）
===========  ==================================================

字段表来自官方 ``msgdb/proto/*.proto``（c2c_40800 / group_40600 /
group_40605 / group_40801 / group_40900），保持「字段号 → (名字, 类型, 重复)」
的映射；未知字段不会被丢弃，会收进结果的 ``_unknown`` 列表。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

__all__ = [
    "FieldDef",
    "MessageDef",
    "Segment",
    "decode_message",
    "parse_40800",
    "parse_40600",
    "parse_40605",
    "parse_40801",
    "parse_40900",
    "MESSAGES",
    "SCHEMA_40800",
    "SCHEMA_MSG_CONTENT",
    "CONTENT_TYPE_NAMES",
    "KIND_TEXT",
    "KIND_IMAGE",
    "KIND_VIDEO",
    "KIND_FILE",
    "KIND_VOICE",
    "KIND_STICKER",
    "KIND_REPLY",
    "KIND_CONTACT",
    "KIND_FORWARD",
    "KIND_LEGACY_FORWARD",
    "KIND_CALL",
    "KIND_SYS",
    "KIND_RED_PACKET",
    "KIND_LOCATION",
    "KIND_UNKNOWN",
]


# ── 语义类型常量 ────────────────────────────────────────────────────────────

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_FILE = "file"
KIND_VOICE = "voice"
KIND_STICKER = "sticker"
KIND_REPLY = "reply"
KIND_CONTACT = "contact"
KIND_FORWARD = "forward"
KIND_LEGACY_FORWARD = "legacy_forward"
KIND_CALL = "call"
KIND_SYS = "sys"
KIND_RED_PACKET = "red_packet"
KIND_LOCATION = "location"
KIND_UNKNOWN = "unknown"

#: content_type(45002) 的中文名
CONTENT_TYPE_NAMES: dict[int, str] = {
    1: "文本",
    2: "图片",
    3: "文件",
    4: "语音",
    5: "视频",
    6: "表情",
    7: "引用",
    8: "灰条",
    9: "红包",
    10: "Ark应用",
    11: "商城表情",
    14: "Markdown",
    16: "合并转发",
    17: "Bot按钮",
    21: "通话记录",
}

#: msg_type(40011) 的中文名
MSG_TYPE_NAMES: dict[int, str] = {
    2: "文本",
    3: "文件",
    5: "回复/名片",
    6: "名片",
    7: "短视频",
    8: "合并转发",
    11: "聊天记录",
    17: "系统",
    19: "通话",
}

#: 附件种类（与 model.Attachment.kind 对齐）
ATTACH_IMAGE = "image"
ATTACH_VIDEO = "video"
ATTACH_FILE = "file"
ATTACH_VOICE = "voice"


# ── Schema 定义 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldDef:
    """一个 protobuf 字段的定义。"""

    number: int
    name: str
    type: str  # int32/int64/uint32/uint64/bool/string/bytes/message/double/...
    repeated: bool = False
    msg_type: str | None = None  # type == "message" 时的子 message 名


@dataclass(frozen=True)
class MessageDef:
    """一个 message 的字段表。"""

    name: str
    fields: dict[int, FieldDef]
    by_name: dict[str, FieldDef] = field(default_factory=dict, repr=False)

    def get(self, name: str) -> FieldDef | None:
        return self.by_name.get(name)


#: 所有已注册 message：名字 → MessageDef
MESSAGES: dict[str, MessageDef] = {}


def _register(name: str, spec: str) -> MessageDef:
    """按文本表注册一个 message。

    文本每行一条：``<字段号> <类型> <名字> [rep] [msg=子类型]``。
    """
    fields: dict[int, FieldDef] = {}
    for raw_line in spec.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        number = int(parts[0])
        ftype = parts[1]
        fname = parts[2]
        repeated = False
        msg_type = None
        for extra in parts[3:]:
            if extra == "rep":
                repeated = True
            elif extra.startswith("msg="):
                msg_type = extra[4:]
        fields[number] = FieldDef(number, fname, ftype, repeated, msg_type)
    definition = MessageDef(name, fields, {f.name: f for f in fields.values()})
    MESSAGES[name] = definition
    return definition


# ── 各 message 的字段表（来自官方 *.proto）──────────────────────────────────

_SUB = ""

_register("Sub", _SUB)

_register(
    "MsgTimestamp",
    """
50202 int64 timestamp
50205 int32 f50205
""",
)

_register(
    "MediaRef",
    """
45417 int64 uid
45527 int32 f45527
45528 message f45528 msg=Sub
45529 int64 f45529
45530 string md5
""",
)

_register(
    "LocationExtra",
    """
45857 message f45857 msg=Sub
45858 message f45858 msg=Sub
45859 message f45859 msg=Sub
45860 message f45860 msg=Sub
45861 int32 f45861
""",
)

_register(
    "ReplyOrigin",
    """
45001 int64 msg_id
45002 int32 content_type
45003 int32 media_sub
45101 string text
45102 int64 text_attr
45105 string f45105
45411 int32 img_width
45412 int32 img_height
45416 int32 f45416
45418 int32 f45418
45424 bytes f45424
47601 int32 video_flag
47602 string video_text
47605 int32 f47605
47606 int32 f47606
47607 bytes f47607
47608 message f47608 msg=Sub
47609 int32 f47609
47611 int32 f47611
47612 message f47612 msg=Sub
47613 int32 f47613
47614 int32 f47614
47615 message f47615 msg=Sub
47616 message f47616 msg=Sub
47617 int32 f47617
47618 int32 f47618
47619 int32 f47619
47620 int32 f47620
47622 int32 f47622
""",
)

_register(
    "ReplyExtension",
    """
48210 message f48210 msg=Sub
48211 int32 f48211
48212 int32 f48212
48213 int32 f48213
48214 string f48214
48215 int32 f48215
48216 int32 f48216
48217 message f48217 msg=Sub rep
48218 message f48218 msg=Sub
48271 string f48271
48272 int32 f48272
48273 int32 f48273
48275 int32 f48275
""",
)

# 40800 消息体（c2c 与 group 共用）
_register(
    "MsgContent",
    """
5        message f5 msg=Sub
40010    int32 f40010
40020    string sender_uid
45001    int64 msg_id
45002    int32 content_type
45003    int32 media_sub
45004    message f45004 msg=Sub
45101    string text
45102    int64 text_attr
45103    int32 f45103
45104    int32 f45104
45105    string f45105
45106    int32 f45106
45108    int32 f45108
45109    int32 f45109
45110    message f45110 msg=Sub
45111    int32 f45111
45112    message f45112 msg=MsgTimestamp
45402    string filename
45403    string filepath
45404    string f45404
45405    int64 filesize
45406    bytes md5_raw
45407    bytes f45407
45408    bytes thumbnail
45409    bytes f45409
45410    int32 f45410
45411    int32 img_width
45412    int32 img_height
45413    int32 f45413
45414    int32 f45414
45415    int32 f45415
45416    int32 f45416
45418    int32 f45418
45419    string file_ext
45421    bytes f45421
45422    string f45422
45423    int32 f45423
45424    bytes f45424
45501    int32 f45501
45503    string file_uuid
45504    bytes cdn_path
45505    int64 expire_ts
45507    int32 f45507
45509    int32 f45509
45510    bytes f45510
45511    int32 f45511
45512    int32 f45512
45513    int32 f45513
45514    int32 f45514
45515    int64 short_ttl_ts
45516    int32 short_ttl_dur
45517    int64 upload_ts
45518    int32 long_ttl_dur
45519    int64 long_ttl_ts
45526    message media_ref msg=MediaRef
45550    int32 f45550
45600    bytes sticker
45801    message f45801 msg=Sub
45802    string cdn_url_1
45803    string cdn_url_2
45804    string cdn_url_3
45805    int32 f45805
45806    int32 f45806
45807    int32 f45807
45812    string local_path
45814    string ntos_path
45815    bytes text_fallback rep
45816    string cdn_host
45817    int32 f45817
45818    message f45818 msg=Sub
45819    message f45819 msg=Sub
45820    message f45820 msg=Sub
45821    int32 f45821
45822    int32 f45822
45823    int32 f45823
45824    message f45824 msg=Sub
45825    int32 f45825
45826    int32 f45826
45827    int32 f45827
45828    message f45828 msg=Sub
45829    int32 f45829
45851    int32 loc_type
45852    int32 loc_f45852
45853    int32 loc_f45853
45854    int32 loc_f45854
45855    int32 loc_f45855
45856    message loc_extra msg=LocationExtra
45862    bytes video_thumb_md5
45863    int32 loc_f45863
45906    int32 nc_category rep
45907    int32 nc_f45907 rep
45909    int32 nc_f45909
45911    int32 nc_f45911
45922    int32 nc_f45922
45923    string nc_text
45924    int32 nc_f45924
45925    bytes nc_avatar
45926    int32 nc_f45926
45954    string f45954
45974    message f45974 msg=Sub
47401    int64 reply_msg_id
47402    int64 reply_msg_seq
47403    int64 reply_msg_time
47404    int64 reply_msg_time_alt
47413    string reply_summary
47421    string reply_group_nickname
47422    int64 reply_source_record_id
47501    int32 ref_type
47502    message ref_f47502 msg=Sub
47601    int32 video_flag
47602    string video_text
47603    string video_width
47604    string video_height
47605    int32 f47605
47606    int32 f47606
47607    string f47607
47608    message f47608 msg=Sub
47609    int32 f47609
47610    int32 f47610
47611    int32 f47611
47612    message f47612 msg=Sub
47613    int32 f47613
47614    int32 f47614
47615    message f47615 msg=Sub
47616    message f47616 msg=Sub
47617    int32 f47617
47618    int32 f47618
47619    int32 f47619
47620    int32 f47620
47622    int32 f47622
47702    int32 nc_relation
47703    string nc_uid_1
47704    string nc_uid_2
47705    string nc_nickname_1
47706    string nc_remark_1
47710    message ref_msg msg=MsgContent
47711    int32 ref_f47711
47713    string ref_f47713
47714    string nc_nickname_2
47715    string nc_remark_2
47901    string fwd_meta
47902    string fwd_token
47904    string fwd_uuid
48151    int32 call_type
48152    int32 call_duration
48153    string call_desc
48154    int32 call_f48154
48155    int32 call_f48155
48156    int32 call_f48156
48157    string call_f48157
48210    message reply_extension msg=ReplyExtension
48217    message reply_items msg=Sub rep
48218    message reply_f48218 msg=Sub
48271    string reply_f48271
48272    int32 reply_f48272
48273    int32 reply_f48273
48275    int32 reply_f48275
48601    string legacy_fwd_resid
48602    string legacy_fwd_xml
48603    string legacy_fwd_uuid
49154    string ext_proto_ver
49155    int64 ext_timestamp
80810    int32 sys_f80810
80824    string sys_f80824
80900    string sys_content
80901    int32 sys_f80901
80902    int32 sys_f80902
80903    bytes sys_f80903
80905    int32 sys_f80905
80908    int32 sys_f80908
80909    int32 sys_f80909
80910    int32 sys_f80910
80935    int32 sys_f80935
80941    string sys_f80941
80942    string sys_f80942
80970    message sys_f80970 msg=Sub rep
80975    int32 sys_f80975
80980    int32 sys_f80980
80981    int32 sys_f80981
80983    message sys_f80983 msg=Sub
80995    int32 sys_f80995
95654    int64 file_thumb_size
""",
)

_register(
    "MsgBody",
    """
40800 message content msg=MsgContent rep
""",
)

# group 40600
_register(
    "Group40600Content",
    """
42211 int64 status_timestamp
42261 int64 marker
""",
)
_register(
    "Group40600Body",
    """
40600 message content msg=Group40600Content
""",
)

# group 40605
_register(
    "Empty",
    "",
)
_register(
    "Group40605Body",
    """
40605 message content msg=Empty
""",
)

# group 40801
_register(
    "Group40801Content",
    """
41501 int64 msg_id
41502 int64 state
41510 int64 f41510
41511 int64 f41511
41512 int64 f41512
41513 int64 f41513
41514 int64 f41514
41516 int64 f41516
41517 int64 f41517
41518 int64 f41518
41519 int64 f41519
41520 int64 f41520
41521 int64 f41521
41522 int64 f41522
41523 int64 f41523
41524 int64 f41524
41525 int64 f41525
41528 int64 f41528
41529 int64 f41529
41530 int64 f41530
41531 int64 f41531
41532 int64 f41532
41533 int64 f41533
41534 int64 f41534
41535 int64 f41535
41536 int64 f41536
41537 int64 f41537
41693 bytes f41693
41694 int64 f41694
41695 int64 f41695
41696 int64 f41696
41697 int64 f41697
41698 int64 f41698
43301 int64 f43301
43302 int64 f43302
52346 bytes f52346
""",
)
_register(
    "Group40801Body",
    """
40801 message content msg=Group40801Content rep
""",
)

# group 40900
_register(
    "Group40900Content",
    """
40001 int64 msg_id
40002 int64 msg_random
40003 int64 msg_seq
40006 int64 server_seq
40008 int32 batch_flag
40009 int32 forward_flag
40010 int32 chat_type
40011 int32 msg_type
40012 int32 subtype
40013 int32 direction
40016 int32 f40016
40020 bytes sender_uid
40021 bytes peer_id
40030 int32 group_id
40033 int32 sender_qq
40041 int32 send_status
40050 int64 msg_time
40058 int64 date_index
40093 string sender_name
40094 bytes f40094
40800 bytes message rep
40801 bytes summary rep
40802 bytes summary2 rep
41999 bytes f41999
40720 int32 f40720
40721 int32 f40721
49082 int32 f49082
""",
)
_register(
    "Group40900Body",
    """
40900 message content msg=Group40900Content rep
""",
)

SCHEMA_40800 = MESSAGES["MsgBody"]
SCHEMA_MSG_CONTENT = MESSAGES["MsgContent"]


# ── wire format 基础 ────────────────────────────────────────────────────────


class ProtoError(ValueError):
    """protobuf 数据损坏。"""


def _read_varint(data: bytes, pos: int) -> tuple[int, int] | None:
    """读一个 varint，返回 ``(值, 新位置)``；数据不完整时返回 None。"""
    result = 0
    shift = 0
    n = len(data)
    while pos < n:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            return None
    return None


def _to_signed(value: int, bits: int) -> int:
    """把无符号 varint 转成二进制补码的有符号数。"""
    value &= (1 << bits) - 1
    limit = 1 << (bits - 1)
    return value - (1 << bits) if value >= limit else value


def _zigzag(value: int) -> int:
    """sint32/sint64 的 zigzag 解码。"""
    return (value >> 1) ^ -(value & 1)


_VARINT_TYPES = {
    "int32",
    "int64",
    "uint32",
    "uint64",
    "bool",
    "enum",
    "sint32",
    "sint64",
}
_FIXED32_TYPES = {"fixed32", "sfixed32", "float"}
_FIXED64_TYPES = {"fixed64", "sfixed64", "double"}


def _resolve(schema: MessageDef | str | dict) -> MessageDef:
    """把 schema 参数（MessageDef / 名字 / {字段号: FieldDef}）统一成 MessageDef。"""
    if isinstance(schema, MessageDef):
        return schema
    if isinstance(schema, str):
        try:
            return MESSAGES[schema]
        except KeyError as exc:
            raise KeyError(f"未知的 message 类型: {schema}") from exc
    if isinstance(schema, dict):
        # 既支持 {字段号: FieldDef}，也支持 {字段名: FieldDef}
        if schema and all(isinstance(k, int) for k in schema):
            fields = dict(schema)
        else:
            fields = {f.number: f for f in schema.values()}
        by_name = {f.name: f for f in fields.values()}
        return MessageDef("<custom>", fields, by_name)
    raise TypeError(f"不支持的 schema 类型: {type(schema)!r}")


def _convert(fdef: FieldDef, wire_type: int, raw: Any, depth: int) -> Any:
    """把 wire 上的原始值转成 Python 值。"""
    ftype = fdef.type
    if ftype in _VARINT_TYPES:
        if wire_type == 0:
            value = int(raw)
        elif wire_type == 2:
            # packed repeated
            values: list[int] = []
            pos = 0
            while pos < len(raw):
                item = _read_varint(raw, pos)
                if item is None:
                    break
                value, pos = item
                values.append(value)
            return _finish_varint_list(ftype, values)
        else:
            raise ProtoError(f"字段 {fdef.name} 期望 varint，wire type={wire_type}")
        return _finish_varint(ftype, value)
    if ftype in _FIXED32_TYPES:
        if wire_type == 5:
            return _finish_fixed32(ftype, raw)
        if wire_type == 2:
            values = []
            for i in range(0, len(raw) - 3, 4):
                values.append(_finish_fixed32(ftype, raw[i : i + 4]))
            return values
        raise ProtoError(f"字段 {fdef.name} 期望 fixed32，wire type={wire_type}")
    if ftype in _FIXED64_TYPES:
        if wire_type == 1:
            return _finish_fixed64(ftype, raw)
        if wire_type == 2:
            values = []
            for i in range(0, len(raw) - 7, 8):
                values.append(_finish_fixed64(ftype, raw[i : i + 8]))
            return values
        raise ProtoError(f"字段 {fdef.name} 期望 fixed64，wire type={wire_type}")
    if ftype == "string":
        if wire_type != 2:
            raise ProtoError(f"字段 {fdef.name} 期望 string，wire type={wire_type}")
        return bytes(raw).decode("utf-8", errors="replace")
    if ftype == "bytes":
        if wire_type != 2:
            raise ProtoError(f"字段 {fdef.name} 期望 bytes，wire type={wire_type}")
        return bytes(raw)
    if ftype == "message":
        if wire_type != 2:
            raise ProtoError(f"字段 {fdef.name} 期望 message，wire type={wire_type}")
        if depth > 12:  # 防止 ref_msg 之类的自引用导致的无限递归
            return {}
        sub = MESSAGES.get(fdef.msg_type or "")
        if sub is None:
            return {}
        return decode_message(raw, sub, depth=depth + 1)
    # 未知类型：原样返回
    return raw


def _finish_varint(ftype: str, value: int) -> Any:
    if ftype == "bool":
        return bool(value)
    if ftype == "int32":
        return _to_signed(value, 32)
    if ftype == "int64":
        return _to_signed(value, 64)
    if ftype == "uint32":
        return value & 0xFFFFFFFF
    if ftype == "uint64":
        return value & 0xFFFFFFFFFFFFFFFF
    if ftype == "sint32":
        return _to_signed(_zigzag(value), 32)
    if ftype == "sint64":
        return _to_signed(_zigzag(value), 64)
    return value  # enum


def _finish_varint_list(ftype: str, values: list[int]) -> list[Any]:
    return [_finish_varint(ftype, v) for v in values]


def _finish_fixed32(ftype: str, raw: bytes) -> Any:
    if ftype == "float":
        return struct.unpack("<f", raw)[0]
    value = int.from_bytes(raw, "little")
    if ftype == "sfixed32":
        return _to_signed(value, 32)
    return value


def _finish_fixed64(ftype: str, raw: bytes) -> Any:
    if ftype == "double":
        return struct.unpack("<d", raw)[0]
    value = int.from_bytes(raw, "little")
    if ftype == "sfixed64":
        return _to_signed(value, 64)
    return value


_PROTO3_DEFAULTS: dict[str, Any] = {
    "int32": 0,
    "int64": 0,
    "uint32": 0,
    "uint64": 0,
    "bool": False,
    "enum": 0,
    "sint32": 0,
    "sint64": 0,
    "float": 0.0,
    "double": 0.0,
    "fixed32": 0,
    "fixed64": 0,
    "sfixed32": 0,
    "sfixed64": 0,
    "string": "",
    "bytes": b"",
}


def decode_message(
    blob: bytes | bytearray | memoryview | None,
    schema: MessageDef | str,
    *,
    only: Iterable[str] | None = None,
    defaults: bool = False,
    depth: int = 0,
) -> dict:
    """把一段 protobuf 字节解码成 dict。

    :param blob:      原始字节
    :param schema:    MessageDef 或 message 名字（如 ``"MsgContent"``）
    :param only:      只要这些字段（按名字），其余字段跳过不存 —— 大数据量下省内存
    :param defaults:  True 时把 proto3 标量默认值补齐（int→0、str→""、bytes→b""）
    :returns:         ``{字段名: 值}``；未知字段收在 ``_unknown`` 里
    """
    if not blob:
        return {}
    msg_def = _resolve(schema)
    only_set = set(only) if only is not None else None
    out: dict = {}
    unknown: list[tuple[int, int, Any]] = []
    data = bytes(blob)
    pos = 0
    n = len(data)

    while pos < n:
        key_item = _read_varint(data, pos)
        if key_item is None:
            break
        key, pos = key_item
        fnum = key >> 3
        wire_type = key & 7
        if fnum == 0:
            break

        if wire_type == 0:
            item = _read_varint(data, pos)
            if item is None:
                break
            raw, pos = item
        elif wire_type == 1:
            if pos + 8 > n:
                break
            raw = data[pos : pos + 8]
            pos += 8
        elif wire_type == 2:
            item = _read_varint(data, pos)
            if item is None:
                break
            length, pos = item
            if length < 0 or pos + length > n:
                break
            raw = data[pos : pos + length]
            pos += length
        elif wire_type == 5:
            if pos + 4 > n:
                break
            raw = data[pos : pos + 4]
            pos += 4
        else:
            # group 类型的 wire type 3/4：NTQQ 不用，直接停
            break

        fdef = msg_def.fields.get(fnum)
        if fdef is None:
            unknown.append((fnum, wire_type, raw))
            continue
        if only_set is not None and fdef.name not in only_set:
            continue

        try:
            value = _convert(fdef, wire_type, raw, depth)
        except ProtoError:
            continue

        if fdef.repeated:
            bucket = out.setdefault(fdef.name, [])
            if isinstance(value, list):
                bucket.extend(value)
            else:
                bucket.append(value)
        else:
            out[fdef.name] = value

    if unknown:
        out["_unknown"] = unknown

    if defaults:
        for fdef in msg_def.fields.values():
            if fdef.name in out:
                continue
            if fdef.repeated:
                out[fdef.name] = []
            elif fdef.type == "message":
                continue
            else:
                out[fdef.name] = _PROTO3_DEFAULTS.get(fdef.type, None)

    return out


def iter_len_delimited(blob: bytes, want_number: int) -> Iterator[bytes]:
    """扫描 wire 流，产出指定字段号的所有 length-delimited 原始字节。"""
    if not blob:
        return
    data = bytes(blob)
    pos = 0
    n = len(data)
    while pos < n:
        item = _read_varint(data, pos)
        if item is None:
            return
        key, pos = item
        fnum = key >> 3
        wire_type = key & 7
        if wire_type == 0:
            item = _read_varint(data, pos)
            if item is None:
                return
            _, pos = item
        elif wire_type == 1:
            pos += 8
            if pos > n:
                return
        elif wire_type == 2:
            item = _read_varint(data, pos)
            if item is None:
                return
            length, pos = item
            if length < 0 or pos + length > n:
                return
            raw = data[pos : pos + length]
            pos += length
            if fnum == want_number:
                yield raw
        elif wire_type == 5:
            pos += 4
            if pos > n:
                return
        else:
            return


# ── Segment ─────────────────────────────────────────────────────────────────


@dataclass
class Segment:
    """消息体里的一个内容段（一个 MsgContent）。"""

    field_no: int  # 外层字段号（40800）
    name: str  # 外层字段名（"content"）
    kind: str  # 语义类型：text/image/video/file/voice/sticker/...
    value: dict  # 该段解码后的字段字典

    # ── 常用字段快捷方式 ──
    @property
    def content_type(self) -> int:
        return int(self.value.get("content_type") or 0)

    @property
    def msg_id(self) -> int:
        return int(self.value.get("msg_id") or 0)

    def seg_text(self) -> str | None:
        """本段的文本（45101），没有则回退到 text_fallback(45815)。"""
        text = self.value.get("text")
        if text:
            return text
        fallbacks = self.value.get("text_fallback") or []
        for item in fallbacks:
            if isinstance(item, (bytes, bytearray)):
                text = bytes(item).decode("utf-8", errors="replace")
            else:
                text = str(item)
            if text:
                return text
        return None

    def seg_type_name(self) -> str:
        """本段的中文类型名，如「图片」「文件」。"""
        return CONTENT_TYPE_NAMES.get(self.content_type, "未知")

    def seg_local_path(self) -> str | None:
        """本地缓存路径（45812）。"""
        path = self.value.get("local_path")
        return path or None

    def seg_filename(self) -> str | None:
        """文件名（45402）。"""
        name = self.value.get("filename")
        return name or None

    def seg_filesize(self) -> int | None:
        """文件大小（45405）。"""
        size = self.value.get("filesize")
        return int(size) if size else None

    def seg_md5(self) -> str:
        """md5_raw(45406) 的 hex 串。"""
        raw = self.value.get("md5_raw") or b""
        return bytes(raw).hex()

    def summarize(self) -> str:
        """一句话摘要，如「[图片]」「[文件:报表.xlsx]」。"""
        kind = self.kind
        if kind == KIND_TEXT:
            return self.seg_text() or ""
        if kind == KIND_IMAGE:
            return "[图片]"
        if kind == KIND_VIDEO:
            return "[视频]"
        if kind == KIND_VOICE:
            return "[语音]"
        if kind == KIND_FILE:
            name = self.seg_filename() or self.value.get("file_ext") or ""
            return f"[文件:{name}]" if name else "[文件]"
        if kind == KIND_STICKER:
            return "[表情]"
        if kind == KIND_REPLY:
            text = self.seg_text() or ""
            return f"[回复]{text}"
        if kind == KIND_CONTACT:
            nick = (
                self.value.get("nc_nickname_1")
                or self.value.get("nc_remark_1")
                or self.value.get("nc_nickname_2")
                or ""
            )
            return f"[名片]{nick}"
        if kind == KIND_FORWARD:
            return "[聊天记录]"
        if kind == KIND_LEGACY_FORWARD:
            return "[合并转发]"
        if kind == KIND_CALL:
            return "[通话]"
        if kind == KIND_RED_PACKET:
            return "[红包]"
        if kind == KIND_LOCATION:
            return "[位置]"
        if kind == KIND_SYS:
            return "[系统消息]"
        return f"[{self.seg_type_name()}]"


def _kind_of(data: dict) -> str:
    """根据 content_type 和伴随字段推断语义类型。"""
    ct = int(data.get("content_type") or 0)
    if ct == 1:
        return KIND_TEXT
    if ct == 2:
        if data.get("img_width"):
            return KIND_IMAGE
        if data.get("f45415") or data.get("video_flag"):
            return KIND_VIDEO
        if data.get("sticker"):
            return KIND_STICKER
        return KIND_IMAGE
    if ct == 3:
        return KIND_FILE
    if ct == 4:
        return KIND_VOICE
    if ct == 5:
        return KIND_VIDEO
    if ct == 6:
        return KIND_STICKER
    if ct == 7:
        return KIND_REPLY
    if ct == 8:
        return KIND_SYS
    if ct == 9:
        return KIND_RED_PACKET
    if ct == 10:
        return KIND_UNKNOWN
    if ct == 11:
        return KIND_STICKER
    if ct == 14:
        return KIND_TEXT
    if ct == 16:
        return KIND_LEGACY_FORWARD
    if ct == 17:
        return KIND_SYS
    if ct == 21:
        return KIND_CALL
    if data.get("sticker"):
        return KIND_STICKER
    if data.get("text"):
        return KIND_TEXT
    if data.get("filename"):
        return KIND_FILE
    return KIND_UNKNOWN


#: db.py 需要的字段（用于 ``only=`` 过滤，减少大库上的开销）
CORE_FIELDS: tuple[str, ...] = (
    "msg_id",
    "content_type",
    "media_sub",
    "text",
    "text_fallback",
    "filename",
    "filepath",
    "file_ext",
    "filesize",
    "md5_raw",
    "img_width",
    "img_height",
    "f45415",
    "video_flag",
    "video_text",
    "sticker",
    "local_path",
    "ntos_path",
    "cdn_url_1",
    "cdn_url_2",
    "cdn_url_3",
    "cdn_host",
    "cdn_path",
    "file_uuid",
    "f45422",
    "f45954",
    "video_thumb_md5",
    "ext_proto_ver",
    "ext_timestamp",
    "fwd_meta",
    "fwd_token",
    "fwd_uuid",
    "legacy_fwd_resid",
    "legacy_fwd_xml",
    "legacy_fwd_uuid",
    "nc_uid_1",
    "nc_uid_2",
    "nc_nickname_1",
    "nc_nickname_2",
    "nc_remark_1",
    "nc_remark_2",
    "ref_f47713",
    "reply_summary",
    "reply_group_nickname",
    "ref_msg",
    "call_type",
    "call_duration",
    "call_desc",
    "sys_f80810",
    "sys_content",
    "loc_type",
    "loc_f45852",
    "loc_f45853",
    "loc_f45854",
    "loc_f45855",
    "f45410",
    "short_ttl_dur",
    "long_ttl_dur",
    "upload_ts",
    "expire_ts",
    "sender_uid",
)


def parse_40800(
    blob: bytes | None,
    *,
    only: Iterable[str] | None = None,
) -> list[Segment]:
    """解析 40800 消息体，返回内容段列表。

    外层是 ``MsgBody { repeated MsgContent content = 40800; }``，
    每个 MsgContent 就是一个 Segment。若数据没有外层包装（直接是 MsgContent），
    会自动降级为「单段」处理。
    """
    if not blob:
        return []
    fields = set(only) if only is not None else None
    segments: list[Segment] = []
    for raw in iter_len_delimited(blob, 40800):
        data = decode_message(raw, "MsgContent", only=fields)
        segments.append(
            Segment(field_no=40800, name="content", kind=_kind_of(data), value=data)
        )
    if segments:
        return segments
    # 降级：整个 blob 就是一个 MsgContent
    data = decode_message(blob, "MsgContent", only=fields)
    if data:
        return [
            Segment(field_no=40800, name="content", kind=_kind_of(data), value=data)
        ]
    return []


def decode_40800(blob: bytes | None, *, only: Iterable[str] | None = None) -> dict:
    """按外层 MsgBody 解码 40800（返回 ``{"content": [ {...}, ... ]}``）。"""
    if not blob:
        return {}
    fields = set(only) if only is not None else None
    contents = []
    for raw in iter_len_delimited(blob, 40800):
        contents.append(decode_message(raw, "MsgContent", only=fields))
    return {"content": contents}


def parse_40600(blob: bytes | None) -> dict:
    """解析 group 的 40600（状态快照）。"""
    if not blob:
        return {}
    out: dict = {}
    for raw in iter_len_delimited(blob, 40600):
        out.update(decode_message(raw, "Group40600Content"))
    return out


def parse_40605(blob: bytes | None) -> dict:
    """解析 group 的 40605（空 message）。"""
    if not blob:
        return {}
    out: dict = {}
    for raw in iter_len_delimited(blob, 40605):
        out.update(decode_message(raw, "Empty"))
    return out


def parse_40801(blob: bytes | None) -> list[dict]:
    """解析 group 的 40801（摘要/状态快照，可多段）。"""
    if not blob:
        return []
    return [
        decode_message(raw, "Group40801Content")
        for raw in iter_len_delimited(blob, 40801)
    ]


def parse_40900(blob: bytes | None) -> list[dict]:
    """解析 group 的 40900（引用/转发缓存，可多段）。"""
    if not blob:
        return []
    return [
        decode_message(raw, "Group40900Content")
        for raw in iter_len_delimited(blob, 40900)
    ]

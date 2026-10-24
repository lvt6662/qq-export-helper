# -*- coding: utf-8 -*-
"""构造测试用的 Chat 列表：3 个会话（1 个单聊、1 个群聊、1 个空会话）。

依赖：仅标准库 + 本工程的 ``qqexport.model``。

用法::

    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
    from make_fixture_chats import make_chats
    chats = make_chats()

覆盖的消息类型：纯文本、图片附件、视频、文件、回复、系统消息、群昵称。
附件带齐 size / local_path / md5 三个字段。

另外提供 ``make_same_name_chats()``：构造同名会话（同名同类型两个 + 同名群聊一个 +
名字自带 ``_2`` 的一个），用于验证重名会话不会互相覆盖。
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from qqexport.model import (  # noqa: E402
    DIRECTION_IN,
    DIRECTION_OUT,
    MSG_TYPE_FILE,
    MSG_TYPE_REPLY,
    MSG_TYPE_SHORT_VIDEO,
    MSG_TYPE_SYS,
    MSG_TYPE_TEXT,
    Attachment,
    Chat,
    Message,
)

# 固定基准时间，保证测试可复现（2026-10-20 20:00:00 本地时间）
BASE_TIME = _dt.datetime(2026, 10, 20, 20, 0, 0)


def _ts(offset_minutes: int) -> int:
    """相对基准时间偏移若干分钟，返回 UNIX 秒。"""
    return int((BASE_TIME + _dt.timedelta(minutes=offset_minutes)).timestamp())


def _att(kind: str, name: str, size: int, md5: str, local_path: str) -> Attachment:
    """构造一个附件。"""
    return Attachment(
        kind=kind,
        name=name,
        size=size,
        local_path=local_path,
        md5=md5,
        url="",
    )


def _msg(
    msg_id: int,
    offset: int,
    direction: int,
    sender: str,
    text: str,
    msg_type: int = MSG_TYPE_TEXT,
    attachments=None,
) -> Message:
    """构造一条消息。"""
    return Message(
        msg_id=msg_id,
        time=_ts(offset),
        direction=direction,
        sender=sender,
        sender_uid="u_%s" % sender,
        msg_type=msg_type,
        content_type=1 if text else 0,
        text=text,
        content=text,
        attachments=list(attachments or []),
        is_mine=(direction == DIRECTION_OUT),
    )


def make_chats() -> list[Chat]:
    """返回 3 个测试会话。"""
    # ---------------- 1) 单聊：7 条消息 ----------------
    single = Chat(
        chat_id="10001",
        name="张小明",
        peer="10001",
        is_group=False,
        source="nt_msg",
    )
    single.messages = [
        _msg(1, 0, DIRECTION_OUT, "我", "你好，世界"),
        _msg(
            2,
            3,
            DIRECTION_IN,
            "张小明",
            "在的，有什么可以帮你？",
            attachments=[
                _att("image", "照片.png", 12900, "a" * 32, r"D:\QQ导出\图片\照片.png")
            ],
        ),
        _msg(
            3,
            5,
            DIRECTION_OUT,
            "我",
            "这是张图片",
            attachments=[
                _att("image", "风景.jpg", 245760, "b" * 32, r"D:\QQ导出\图片\风景.jpg")
            ],
        ),
        _msg(
            4,
            9,
            DIRECTION_IN,
            "张小明",
            "视频文件也发你了",
            msg_type=MSG_TYPE_SHORT_VIDEO,
            attachments=[
                _att("video", "小视频.mp4", 3355443, "c" * 32, r"D:\QQ导出\视频\小视频.mp4")
            ],
        ),
        _msg(
            5,
            12,
            DIRECTION_OUT,
            "我",
            "文档收到了吗",
            msg_type=MSG_TYPE_FILE,
            attachments=[
                _att("file", "需求说明.docx", 51200, "d" * 32, r"D:\QQ导出\文件\需求说明.docx")
            ],
        ),
        _msg(6, 15, DIRECTION_IN, "系统", "对方撤回了一条消息", msg_type=MSG_TYPE_SYS),
        _msg(
            7,
            20,
            DIRECTION_OUT,
            "我",
            "好的，多谢",
            msg_type=MSG_TYPE_REPLY,
        ),
    ]

    # ---------------- 2) 群聊：6 条消息（含群昵称） ----------------
    group = Chat(
        chat_id="88012345",
        name="产品研发群",
        peer="",
        is_group=True,
        source="nt_msg",
    )
    group.messages = [
        _msg(101, 30, DIRECTION_IN, "王强(研发小王)", "大家好，我是新来的"),
        _msg(102, 32, DIRECTION_OUT, "我", "欢迎欢迎，请多指教"),
        _msg(
            103,
            35,
            DIRECTION_IN,
            "王强(研发小王)",
            "这是上周的会议纪要",
            msg_type=MSG_TYPE_FILE,
            attachments=[
                _att("file", "会议纪要.docx", 81920, "e" * 32, r"D:\QQ导出\文件\会议纪要.docx")
            ],
        ),
        _msg(104, 40, DIRECTION_IN, "李婷(测试小李)", "纪要没问题", msg_type=MSG_TYPE_REPLY),
        _msg(
            105,
            45,
            DIRECTION_OUT,
            "我",
            "原型图更新了",
            attachments=[
                _att("image", "原型图.png", 1048576, "f" * 32, r"D:\QQ导出\图片\原型图.png")
            ],
        ),
        _msg(106, 50, DIRECTION_IN, "系统", "「王强(研发小王)」撤回了一条消息", msg_type=MSG_TYPE_SYS),
    ]

    # ---------------- 3) 空会话：0 条消息 ----------------
    empty = Chat(
        chat_id="10002",
        name="空会话",
        peer="10002",
        is_group=False,
        source="backup",
    )
    empty.messages = []

    return [single, group, empty]


def total_messages(chats=None) -> int:
    """测试用：统计消息总数。"""
    chats = make_chats() if chats is None else chats
    return sum(c.count for c in chats)


def make_same_name_chats() -> list[Chat]:
    """构造 4 个「同名」会话，用于验证重名会话不会互相覆盖。

    组成：
      * 两个同名单聊「同名会话」（chat_id 不同）——文件应自动加 ``_2``/``_3`` 后缀；
      * 一个同名群聊「同名会话」——类型前缀不同，文件名天然不同；
      * 一个单聊「同名会话_2」——它的原始主名会被预留，
        所以前面那个同名单聊只能拿到 ``_3``，两者不会互相覆盖。
    """
    def _mk(chat_id: str, name: str, is_group: bool, text: str, offset: int) -> Chat:
        chat = Chat(
            chat_id=chat_id,
            name=name,
            peer="" if is_group else chat_id,
            is_group=is_group,
            source="nt_msg",
        )
        chat.messages = [_msg(int(chat_id), offset, DIRECTION_OUT, "我", text)]
        return chat

    return [
        _mk("90001", "同名会话", False, "第一个同名会话", 0),
        _mk("90002", "同名会话", False, "第二个同名会话", 1),
        _mk("90003", "同名会话", True, "群聊里的同名会话", 2),
        _mk("90004", "同名会话_2", False, "名字里自带 _2 的会话", 3),
    ]


if __name__ == "__main__":
    cs = make_chats()
    print("会话数：%d" % len(cs))
    for c in cs:
        print(" - %s %s：%d 条消息" % ("群聊" if c.is_group else "单聊", c.name, c.count))
    print("消息总数：%d" % total_messages(cs))

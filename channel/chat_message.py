"""
本类表示聊天消息，用于对itchat和wechaty的消息进行统一的封装。

填好必填项(群聊6个，非群聊8个)，即可接入ChatChannel，并支持插件，参考TerminalChannel

ChatMessage
msg_id: 消息id (必填)
create_time: 消息创建时间

ctype: 消息类型 : ContextType (必填)
content: 消息内容, 如果是声音/图片，这里是文件路径 (必填)

from_user_id: 发送者id (必填)
from_user_nickname: 发送者昵称
to_user_id: 接收者id (必填)
to_user_nickname: 接收者昵称

other_user_id: 对方的id，如果你是发送者，那这个就是接收者id，如果你是接收者，那这个就是发送者id，如果是群消息，那这一直是群id (必填)
other_user_nickname: 同上

is_group: 是否是群消息 (群聊必填)
is_at: 是否被at

- (群消息时，一般会存在实际发送者，是群内某个成员的id和昵称，下列项仅在群消息时存在)
actual_user_id: 实际发送者id (群聊必填)
actual_user_nickname：实际发送者昵称
self_display_name: 自身的展示名，设置群昵称时，该字段表示群昵称

- (引用消息相关字段)
reply_to_message_id: 被引用消息的ID
reply_to_message_type: 被引用消息的类型 (ContextType)
reply_to_content: 被引用消息的内容
reply_to_user_id: 被引用消息的发送者ID
reply_to_user_nickname: 被引用消息的发送者昵称
reply_to_media_path: 被引用媒体的本地路径 (仅媒体类型消息)
reply_to_media_url: 被引用媒体的URL (仅媒体类型消息)
reply_to_media_id: 被引用媒体的ID (仅媒体类型消息)
reply_to_metadata: 被引用消息的元数据，字典类型

_prepare_fn: 准备函数，用于准备消息的内容，比如下载图片等,
_prepared: 是否已经调用过准备函数
_rawmsg: 原始消息对象

"""


class ChatMessage(object):
    msg_id = None
    create_time = None

    ctype = None
    content = None

    from_user_id = None
    from_user_nickname = None
    to_user_id = None
    to_user_nickname = None
    other_user_id = None
    other_user_nickname = None
    my_msg = False
    self_display_name = None

    is_group = False
    is_at = False
    actual_user_id = None
    actual_user_nickname = None
    at_list = None

    # 引用消息相关字段
    reply_to_message_id = None
    reply_to_message_type = None
    reply_to_content = None
    reply_to_user_id = None
    reply_to_user_nickname = None
    reply_to_media_path = None
    reply_to_media_url = None
    reply_to_media_id = None
    reply_to_metadata = None

    _prepare_fn = None
    _prepared = False
    _rawmsg = None

    def __init__(self, _rawmsg):
        self._rawmsg = _rawmsg

    def prepare(self):
        if self._prepare_fn and not self._prepared:
            self._prepared = True
            self._prepare_fn()

    def __str__(self):
        return "ChatMessage: id={}, create_time={}, ctype={}, content={}, from_user_id={}, from_user_nickname={}, to_user_id={}, to_user_nickname={}, other_user_id={}, other_user_nickname={}, is_group={}, is_at={}, actual_user_id={}, actual_user_nickname={}, at_list={}, reply_to_message_id={}".format(
            self.msg_id,
            self.create_time,
            self.ctype,
            self.content,
            self.from_user_id,
            self.from_user_nickname,
            self.to_user_id,
            self.to_user_nickname,
            self.other_user_id,
            self.other_user_nickname,
            self.is_group,
            self.is_at,
            self.actual_user_id,
            self.actual_user_nickname,
            self.at_list,
            self.reply_to_message_id
        )

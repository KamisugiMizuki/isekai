"""版本与协议常量（单一真源）。

三类版本互不替代（DESIGN.md §5.7 版本职责分离）：
- DATA_FORMAT_VERSION：存储可读性
- RULES_VERSION：世界事实推进与确定性复算
- 生成器 / 提示词 / 文本模型指纹：只负责新文本产物
"""

APP_VERSION = "0.1.0"

#: UMP 信封 ump 字段取值；协商只认主版本
UMP_VERSION = "1.0"
UMP_MAJOR = "1"

DATA_FORMAT_VERSION = "0.1"
RULES_VERSION = "0.1"

#: 导出容器格式版本（导入兼容性判定的主版本；主版本不同需要转换工具）
CONTAINER_FORMAT = "isekai.instance"
CONTAINER_VERSION = "1.0"

#: 本端能力声明：导入件要求的每项能力都必须在此列表内（WORLD_SETTING §7.3）
CAPABILITIES = (
    "world.package.v1",
    "cards.v1",
    "instance.v1",
    "message.delivery.v1",
)

#: 协议限额（默认值；握手时与通道取交集）
DEFAULT_MAX_TEXT_LEN = 4000
DEFAULT_MAX_PARTS = 10

MAX_FRAME_BYTES = 1 << 20  # 1 MiB：恶意大帧直接断，不进解析
PROTOCOL_ERROR_LIMIT = 5  # 连续协议错误上限，超过断开连接
HANDSHAKE_TIMEOUT_S = 10.0

#: 容量与限速（CHANNEL_PLUGIN_SPEC §3.2「帧、队列与日志有容量上限；持续无效消息或堵塞输出不能耗尽核心」）
#: 都是开发者级旋钮（只在 config.yaml，不进设置面）：单机场景下取够宽的值，正常客户端碰不到
DEFAULT_MAX_CONNECTIONS = 32        # 在线通道连接数上限：超出只拒新连接，不动在线的
DEFAULT_MAX_QUEUED_INBOUND = 32     # 单会话排队入站上限：满了回 rate_limited/overloaded 让客户端退避，不丢弃已接受的
DEFAULT_RATE_LIMIT_MSGS = 60        # 每连接每窗口允许的入站帧数
DEFAULT_RATE_LIMIT_WINDOW_S = 10.0


def generator_fingerprint(*, segments: tuple, hints: tuple, model: str = "") -> str:
    """生成器 / 提示词 / 文本模型指纹：只负责新文本产物的边界（§5.7）。

    文本产物（世界包 / 角色卡）的「谁在什么时候用什么生成」要可追溯，但**不**参与
    事实推进或存储可读性的判定。同一个生成器与同一模型必须得到同一个值。
    """
    import hashlib

    payload = "|".join([*map(str, segments), *map(str, hints), str(model or "")])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

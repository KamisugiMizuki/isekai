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


def generator_fingerprint(*, segments: tuple, hints: tuple, model: str = "") -> str:
    """生成器 / 提示词 / 文本模型指纹：只负责新文本产物的边界（§5.7）。

    文本产物（世界包 / 角色卡）的「谁在什么时候用什么生成」要可追溯，但**不**参与
    事实推进或存储可读性的判定。同一个生成器与同一模型必须得到同一个值。
    """
    import hashlib

    payload = "|".join([*map(str, segments), *map(str, hints), str(model or "")])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

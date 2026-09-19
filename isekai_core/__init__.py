"""isekai 核心进程。

阶段 0：统一消息协议（UMP v1）、最小持久会话、通道宿主与内建聊天客户端。
世界设定层、运行层、事件引擎、记忆与角色卡在后续阶段接入（见 docs/DESIGN.md §6）。
"""

from .version import APP_VERSION, DATA_FORMAT_VERSION, RULES_VERSION, UMP_VERSION

__all__ = ["APP_VERSION", "DATA_FORMAT_VERSION", "RULES_VERSION", "UMP_VERSION"]

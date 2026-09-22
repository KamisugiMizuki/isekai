"""OC 故事层（OC_STORY_LAYER_SPEC）：面向普通创作者的联络语义、产品状态与版本编排。

分层：

- `classify`：输入分类（六类主类别，分类不改变权限）；
- `state`：底层结果 → 产品状态与返回信封（§3.5 / §4.4 / §七）；
- `expression`：故事表达契约与讲述边界（§五）；
- `view`：用户可见面投影与黑箱白名单（§六）；
- `service`：编排（首次进入 / 联络轮次 / 分支与恢复）。

本层不拥有世界事实，也不复制会话核心状态机：所有写入仍走会话核心与 WorldRuntime 对外接口。
"""

from . import classify, expression, state, view
from .service import StoryService

__all__ = ["StoryService", "classify", "expression", "state", "view"]

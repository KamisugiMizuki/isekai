"""Writing Assistant（WRITING_ASSISTANT_SPEC）：大纲、候选、草稿与偏离决定的产品语义。

分层：

- `outline`：分层约束（主题 / 必达 / 禁止 / 弧线 / 节奏 / 可变素材）、条目状态机、偏离判定（纯逻辑）；
- `candidates`：候选生命周期（纯逻辑）——批准不等于已提交；
- `service`：编排（大纲与绑定、只读观察、候选提出与提交、GM 直接变化、分支试演）。

本层不拥有世界真值：世界事实仍只有 `runtime.change.commit`（或 TRPG 联合提交）能写。
"""

from . import candidates, outline
from .service import WritingError, WritingService

__all__ = ["WritingService", "WritingError", "candidates", "outline"]

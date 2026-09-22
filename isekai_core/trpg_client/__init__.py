"""TRPG 客户端层（TRPG_CLIENT_SPEC）：产品面、行动闭环、状态文案、结果表达与显示闸门。

分层：
  - `states.py`      §7 用户可见状态表与文案（纯数据 + 纯函数）
  - `views.py`       §5 / §6 / §7.2 / §十二 四个产品面 + 受众闭集 + 显示闸门
  - `expression.py`  §8 / §9 / §10 / §11 结果四层、失败语义、待选择卡、双轨时间
  - `draft.py`       §6.1 行动草稿（一次低成本判断调用 + 不猜的缺口判定）
  - `service.py`     §3 / §4 / §14 / §15 / §16 编排：进入、行动、选择、重试、GM 辅助

客户端不新增旁路 API、不复制世界真值、不建第二套秘密库（§15 / §18.11）。
"""

from __future__ import annotations

from .service import TrpgClient, TrpgClientError, default_workspace, merge_workspace

__all__ = ["TrpgClient", "TrpgClientError", "default_workspace", "merge_workspace"]

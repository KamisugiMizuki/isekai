"""阶段 3 文档补充：自主提案 + 环境事实状态。"""

from __future__ import annotations

import pathlib

r = pathlib.Path("README.md")
t = r.read_text(encoding="utf-8")

anchor = "- **调用账本**（§2.8 的运行时形态）"
assert anchor in t, "README 账本锚点不在"
add = (
    "- **角色自主提案**（§11.3）：核心在激活线上让模型替角色想一步——依据只给她已知的说法 / 观察 / 后果，\n"
    "  行动类型必须在闭集、目标必须在她可知的集合里（不合规直接丢弃，不替她生成未获知的目标）；\n"
    "  同一世界日不重复提案、未竟之事上限 3 件、受现实日预算约束；提案期间世界推进过就按最新世代提交，\n"
    "  写入被拒不谎报（真机踩到过「报了 1 条、库里没有」）。\n"
    "- **环境事实状态**（§11.2）：世界包声明类型 / 单位 / 取值域 / 初始值 / 变化来源 / 观察条件，未声明即无真值；\n"
    "  变化只来自声明的自然来源（按世界日确定推进）与事件效果，与环境同批提交；\n"
    "  认知接口只返回角色能观察到的投影，未列出的环境她不知道，只能给主观感受。\n"
)
t = t.replace(anchor, add + anchor, 1)

old_sum = "角色经历与素材资格、创建期历史回填（见「事件引擎」一节）；记忆子系统自后续阶段实现。"
new_sum = (
    "角色经历与素材资格、创建期历史回填、角色自主提案与环境事实状态（见「事件引擎」一节）；"
    "记忆子系统自后续阶段实现。"
)
assert old_sum in t
t = t.replace(old_sum, new_sum, 1)

old_mod = "事件引擎（`events.py`） |"
new_mod = "事件引擎（`events.py`）、角色自主提案（`planning.py`）、环境事实状态（`environment.py`） |"
assert old_mod in t
t = t.replace(old_mod, new_mod, 1)
r.write_text(t, encoding="utf-8")

d = pathlib.Path("docs/DESIGN.md")
dt = d.read_text(encoding="utf-8")
mark = "**阶段 3（事件引擎）**"
i = dt.find(mark)
assert i >= 0, "DESIGN 阶段 3 锚点不在"
line_end = dt.find("\n", i)
extra = (
    "\n- 角色自主提案（后台一次模型调用 → 闭集与可知目标校验 → 与水位同批提交、按最新世代）"
    "；§11.2 环境事实状态（声明驱动 + 自然变化 + 事件改值 + 观察投影）"
)
dt = dt[:line_end] + extra + dt[line_end:]
d.write_text(dt, encoding="utf-8")
print("docs 写入完成")

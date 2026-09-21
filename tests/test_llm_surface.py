"""LLM 表面积（省钱的两条不变式）。

① **世界模拟路径不含 LLM**：advance / 批次收集 / 事件 / 制度 / 环境 / 意图修订全是确定性的。
   这不是巧合而是设计约束（世界推进不该按 NPC 数量乘上模型调用），所以锁成断言。
② **提示前缀要能命中缓存**：DeepSeek 的缓存是严格前缀匹配，常量必须在最前、
   每次会变的东西（时刻 / 名字 / 材料 / 意图）放后面。同一段 system 在两次调用间字节相同
   才吃得到命中价——反过来（时刻打头）每次都是整段全价。
"""

from __future__ import annotations

import ast
import pathlib

from isekai_core.runtime import drafts, memory, narrative, service

#: 世界模拟路径：这些函数在推进世界时间 / 生成事实时被调用，必须保持确定性
SIMULATION_PATHS = (
    "advance",
    "_collect_batch",
    "_world_event_rows",
    "_death_rows",
    "_revise_intents",
    "_institution_rows",
    "_environment_rows",
    "_propagate_and_clear",
)


def _function_source(name: str) -> str:
    src = pathlib.Path(service.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"service.py 里没有 {name}")


def test_world_simulation_path_has_no_llm_calls() -> None:
    """世界时间推进链里一次模型调用都不该有：1734 个事件对应 0 次模拟侧调用。"""
    for name in SIMULATION_PATHS:
        body = _function_source(name)
        assert "llm" not in body, f"{name} 里出现了 llm：世界模拟要保持确定性、不按角色数乘调用"


def test_extraction_prompt_prefix_is_shared_across_calls() -> None:
    """规则块在所有提取调用间字节相同；角色名 / 时刻 / 材料都在 user 段。"""
    first = memory.extraction_prompt(
        name="凛",
        world_label="第一天 清晨",
        items=[{"ref": "m1", "text": "她看到门外的脚印", "source": "对话"}],
        existing=[{"id": "mem-1", "text": "她记得旧事"}],
    )
    second = memory.extraction_prompt(
        name="许眠",
        world_label="第九天 深夜",
        items=[{"ref": "m2", "text": "别的事", "source": "观察"}],
    )
    assert first[0]["role"] == "system" and first[1]["role"] == "user"
    assert first[0]["content"] == second[0]["content"], "system 段必须与角色 / 时刻 / 材料无关"
    assert "第一天 清晨" not in first[0]["content"]
    assert "第一天 清晨" in first[1]["content"] and "凛" in first[1]["content"]


def test_proposal_prompt_keeps_the_intent_out_of_system() -> None:
    """GM 意图翻译：允许清单在 system（同一世界包内稳定），意图与世界时刻在 user。"""
    allowed = {"institution_state": ["off-1"], "environment": ["env-1"]}
    first = drafts.proposal_prompt(
        intent="换掉守夜人", world_label="第一天 清晨", allowed=allowed, channels=["public"]
    )
    second = drafts.proposal_prompt(
        intent="下场大雨", world_label="第二天 下午", allowed=allowed, channels=["public"]
    )
    assert first[0]["content"] == second[0]["content"]
    assert "换掉守夜人" in first[1]["content"] and "换掉守夜人" not in first[0]["content"]


def test_audit_prompt_prefix_stays_constant() -> None:
    """忠实度审计本来就是 system 常量 + user 可变——别在改动里把它弄反。"""
    unit = {"materials": [{"stance": "亲历", "source": "s1", "text": "她看到脚印"}], "activity": "值夜"}
    first = narrative.audit_request(unit, "我看到了")
    second = narrative.audit_request({**unit, "activity": "巡夜"}, "另一句话")
    assert first[0]["content"] == second[0]["content"]
    assert first[1]["content"] != second[1]["content"]

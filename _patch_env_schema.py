"""补丁：环境类型的效果闭集 + 校验 + 示例包声明。"""

from __future__ import annotations

import pathlib

# ---------- 1) 效果闭集加环境类；校验环境类型与取值 ----------
v = pathlib.Path("isekai_core/world/validate.py")
t = v.read_text(encoding="utf-8")
t = t.replace(
    '''    "institution_state": "制度状态",
}''',
    '''    "institution_state": "制度状态",
    "environment_state": "环境状态（只改已声明的环境类型与取值域）",
}''',
    1,
)

old = '''                expiry = effect.get("expiry")
                if expiry not in EXPIRY_KINDS:'''
new = '''                if str(effect.get("kind")) == "environment_state":
                    env = _environment_type(package, str(effect.get("target") or ""))
                    if env is None:
                        errors.append(
                            f"{t_where}.effects[{e_index}].target: 环境效果必须指向已声明的环境类型"
                        )
                    elif effect.get("value") not in (env.get("values") or []):
                        errors.append(
                            f"{t_where}.effects[{e_index}].value: 取值必须是该环境类型取值域内的值"
                        )
                expiry = effect.get("expiry")
                if expiry not in EXPIRY_KINDS:'''
assert t.count(old) == 1
t = t.replace(old, new, 1)

helper = '''def _environment_type(package: dict[str, Any], type_id: str) -> dict[str, Any] | None:
    environment = package.get("environment") if isinstance(package.get("environment"), dict) else {}
    for item in environment.get("types") or []:
        if isinstance(item, dict) and str(item.get("id")) == type_id:
            return item
    return None


def _validate_environment(package: dict[str, Any], errors: list[str]) -> None:
    """环境类型：观察条件之外的机器可读部分——观察者名单（可选，缺省即无人可见）。"""
    environment = package.get("environment") if isinstance(package.get("environment"), dict) else {}
    types = environment.get("types") if isinstance(environment.get("types"), list) else []
    known = _all_ids(package)
    for index, item in enumerate(types):
        if not isinstance(item, dict):
            continue
        where = f"environment.types[{index}]"
        observers = item.get("observers")
        if observers is None:
            continue
        names = list(observers.values()) if isinstance(observers, dict) else observers
        if not isinstance(observers, (list, dict)):
            errors.append(f"{where}.observers: 必须是列表（角色 / 种族 / 地区标识）或按观察者的映射")
            continue
        for name in names:
            if not isinstance(name, str):
                continue
            value = name.strip()
            if value in ("all", "亲历"):
                continue
            if value not in known:
                errors.append(f"{where}.observers: 引用不存在的标识 {value!r}")


'''
anchor = "def _validate_events("
assert t.count(anchor) == 1
t = t.replace(anchor, helper + anchor, 1)
old_call = "    _validate_event_calendar(package, errors)"
assert t.count(old_call) == 1
t = t.replace(old_call, old_call + "\n    _validate_environment(package, errors)", 1)
v.write_text(t, encoding="utf-8")

# ---------- 2) 示例包：声明两种环境类型 + 一个环境效果示例 ----------
e = pathlib.Path("isekai_core/world/example.py")
et = e.read_text(encoding="utf-8")
old_env = '''    package["environment"] = {"types": []}'''
if et.count(old_env) != 1:
    import re

    match = re.search(r'\n    package\["environment"\] = \{[^\n]*\}\n', et)
    assert match, "没找到 environment 段落"
    old_env = match.group(0).strip()
    replacement = '''    package["environment"] = {
        "types": [
            {
                "id": "env-1",
                "name": "潮位",
                "unit": "尺",
                "values": [0, 1, 2, 3, 4, 5],
                "initial": 2,
                "sources": ["natural:潮汐"],
                "observe": "在滩口值守且有水位尺时能读到刻线；城内只听到信报转述，不能说具体数字",
                "observers": ["rl-1"],
                "expiry": "natural_recovery",
            },
            {
                "id": "env-2",
                "name": "风信",
                "unit": "向",
                "values": ["北", "东", "南", "西"],
                "initial": "北",
                "sources": ["event"],
                "observe": "在开阔处能感到风向；在屋里只能从别人的话里知道",
                "observers": {"rl-1": "她站在滩口，能直接感到风向"},
                "expiry": "until_cleared",
            },
        ]
    }'''
    et = et.replace(old_env, replacement, 1)
else:
    et = et.replace(old_env, '''    package["environment"] = {
        "types": [
            {
                "id": "env-1",
                "name": "潮位",
                "unit": "尺",
                "values": [0, 1, 2, 3, 4, 5],
                "initial": 2,
                "sources": ["natural:潮汐"],
                "observe": "在滩口值守且有水位尺时能读到刻线；城内只听到信报转述，不能说具体数字",
                "observers": ["rl-1"],
                "expiry": "natural_recovery",
            }
        ]
    }''', 1)
e.write_text(et, encoding="utf-8")
print("validator / example 已更新")

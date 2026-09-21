"""设置面多段写入（DESKTOP_SPEC §3.3）：memory / commit / backup 三种日常可调项。

判据：写进 config.yaml 后**服务侧副本同步**（改了真生效）、读回一致、非法键被拒且不改动、
开发者专用键不在白名单里。
"""

from __future__ import annotations

import pytest

from conftest import open_mgmt, running_core
from isekai_core.ump import UmpError
from test_runtime import make_instance, store, world  # noqa: F401


async def test_settings_sections_round_trip(tmp_path) -> None:
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        before = await mgmt.call("settings.get")
        assert before["memory"]["mode"] in ("chat", "separate")
        assert "api_key" in before["backup"] or "interval_hours" in before["backup"]

        out = await mgmt.call(
            "settings.set", commit={"auto_enabled": False, "minutes": 15, "events": 7}
        )
        assert out["commit"] == {"auto_enabled": False, "minutes": 15, "events": 7}
        world_service = getattr(h.runtime, "world", None) or h.runtime.service
        assert getattr(world_service, "autocommit_enabled", None) is False, (
            "运行层服务的运行时副本必须同步，否则配置改了不生效"
        )
        again = await mgmt.call("settings.get")
        assert again["commit"]["minutes"] == 15 and again["commit"]["events"] == 7

        backed = await mgmt.call("settings.set", backup={"interval_hours": 6, "keep": 3})
        assert backed["backup"]["interval_hours"] == 6 and backed["backup"]["keep"] == 3


async def test_settings_set_rejects_unknown_and_developer_keys(tmp_path) -> None:
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        with pytest.raises(UmpError) as exc:
            await mgmt.call("settings.set", runtime={"rate_max": 10})
        assert "不开放" in str(exc.value)
        with pytest.raises(UmpError) as exc2:
            await mgmt.call("settings.set", commit={"nonsense": 1})
        assert "nonsense" in str(exc2.value)
        after = await mgmt.call("settings.get")
        assert after["commit"]["minutes"] == 60, "被拒的写入不得改动原值"

"""插件宿主（CHANNEL_PLUGIN_SPEC §3.1–§3.4）。

判据：扫描只读清单不跑代码；启用要握手成功才算运行；停用不留孤儿；崩溃不重启风暴；
子进程环境不继承核心凭据；卸载保留核心历史。
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import running_core
from isekai_core import plugins

STUBS = Path(__file__).resolve().parent / "plugin_stubs"


def _stub(name: str) -> str:
    return (STUBS / name).read_text(encoding="utf-8")


def _write_plugin(folder, plugin_id: str, source: str, *, entry_name: str = "main.py") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / entry_name).write_text(source, encoding="utf-8")
    (folder / "manifest.json").write_text(
        json.dumps(
            {"id": plugin_id, "name": f"{plugin_id} 插件", "version": "0.1.0", "ump": "1.x",
             "entry": ["python", entry_name], "description": "测试用", "author": "测试"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_scan_reads_manifest_without_running_code(tmp_path) -> None:
    folder = tmp_path / "plugins" / "echo"
    _write_plugin(folder, "echo-plugin", _stub("echo.py"))
    found = plugins.scan(tmp_path / "plugins")
    assert [item["id"] for item in found] == ["echo-plugin"]
    assert found[0]["errors"] == [] and found[0]["entry"] == ["python", "main.py"]
    assert not (folder / "seen.json").exists(), "扫描只读清单，绝不运行代码"

    (folder / "manifest.json").write_text("{ 坏清单", encoding="utf-8")
    broken = plugins.scan(tmp_path / "plugins")
    assert broken and broken[0]["errors"], broken


async def test_enable_disable_and_minimal_environment(tmp_path) -> None:
    async with running_core(tmp_path) as h:
        folder = tmp_path / "plugins" / "echo"
        _write_plugin(folder, "echo-plugin", _stub("echo.py"))
        host = plugins.PluginHost(cfg=h.cfg, store=h.runtime.store, server=h.runtime.server, folder=tmp_path / "plugins")

        result = await host.enable("echo-plugin", timeout=25.0)
        assert result.get("enabled") is True, result
        seen = json.loads((folder / "seen.json").read_text(encoding="utf-8"))
        assert seen["state"] == "ready", seen
        assert seen["leaked"] == [], f"子进程环境不许带核心凭据：{seen['leaked']}"
        assert "ISEKAI_PLUGIN_CREDENTIAL" in seen["env_keys"], "该插件自己的凭据要给"
        assert str(h.runtime.store.plugin_get("echo-plugin")["state"]) == "running"
        listing = {item["id"]: item for item in host.list_plugins()}
        assert listing["echo-plugin"]["state"] == "running"

        stopped = await host.disable("echo-plugin")
        assert stopped["state"] == "stopped" and "echo-plugin" not in host._running
        assert str(h.runtime.store.plugin_get("echo-plugin")["state"]) == "stopped"


async def test_crash_is_marked_and_not_restarted(tmp_path) -> None:
    async with running_core(tmp_path) as h:
        folder = tmp_path / "plugins" / "boom"
        _write_plugin(folder, "boom-plugin", _stub("crash.py"))
        host = plugins.PluginHost(cfg=h.cfg, store=h.runtime.store, server=h.runtime.server, folder=tmp_path / "plugins")
        result = await host.enable("boom-plugin", timeout=25.0)
        assert result.get("enabled") is False and result["state"] == "failed", result
        assert "立刻退出" in str(result.get("note") or ""), "stderr 摘要要带上，好定位"
        assert "boom-plugin" not in host._running, "崩溃后不自动重启（不重启风暴）"
        assert str(h.runtime.store.plugin_get("boom-plugin")["state"]) == "failed"


async def test_uninstall_keeps_core_history(tmp_path) -> None:
    async with running_core(tmp_path) as h:
        folder = tmp_path / "plugins" / "echo"
        _write_plugin(folder, "echo-plugin", _stub("echo.py"))
        host = plugins.PluginHost(cfg=h.cfg, store=h.runtime.store, server=h.runtime.server, folder=tmp_path / "plugins")
        world = h.runtime.world
        assert world is not None
        info, timeline_id, _character = _instance(h, world)
        await host.enable("echo-plugin", timeout=25.0)
        out = await host.uninstall("echo-plugin")
        assert out["uninstalled"] == "echo-plugin"
        assert h.runtime.store.plugin_get("echo-plugin") is None, "登记移除"
        assert h.runtime.store.instance_get(info["id"]) is not None
        assert h.runtime.store.timeline_list(info["id"]), "核心会话与角色历史保留"


async def test_reference_plugin_handshakes(tmp_path) -> None:
    """参考实现（examples/channel_plugin_reference.py）能被真宿主拉起来并握手成功。"""
    async with running_core(tmp_path) as h:
        folder = tmp_path / "plugins" / "reference"
        folder.mkdir(parents=True)
        source = (Path(__file__).resolve().parent.parent / "examples" / "channel_plugin_reference.py").read_text(
            encoding="utf-8"
        )
        (folder / "main.py").write_text(source, encoding="utf-8")
        (folder / "manifest.json").write_text(
            json.dumps({"id": "reference-plugin", "name": "参考通道", "version": "0.1.0", "ump": "1.x",
                        "entry": ["python", "main.py"], "description": "参考实现", "author": "isekai"},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        host = plugins.PluginHost(cfg=h.cfg, store=h.runtime.store, server=h.runtime.server, folder=tmp_path / "plugins")
        result = await host.enable("reference-plugin", timeout=25.0)
        assert result.get("enabled") is True, result
        assert str(h.runtime.store.plugin_get("reference-plugin")["state"]) == "running"
        assert (await host.disable("reference-plugin"))["state"] == "stopped"


def _instance(harness, world):
    from samples import sample_card, sample_package
    from isekai_core.world.instances import create_instance

    package = sample_package()
    store = harness.runtime.store
    info = create_instance(store, package, [sample_card(package)])
    world.ensure_instance(info["id"], now_real=1.7e9)
    timelines = store.timeline_list(info["id"])
    return info, str(timelines[0]["id"]), None


async def test_uninstall_also_drops_the_channel_binding(tmp_path) -> None:
    """§3.2 卸载 = 移除插件登记 **与绑定**；会话 / 消息 / 角色历史一概不动。"""
    async with running_core(tmp_path) as h:
        folder = tmp_path / "plugins"
        _write_plugin(folder, "echo-plugin", _stub("echo.py"))
        host = plugins.PluginHost(cfg=h.cfg, store=h.runtime.store, server=h.runtime.server, folder=folder)
        await host.enable("echo-plugin")
        channel = h.runtime.store.channel_by_name("echo-plugin")
        assert channel is not None and h.runtime.store.thread_list(channel["id"]) == []
        h.runtime.store.thread_bind(channel["id"], "dm-1", "ss-keep")
        sessions_before = h.runtime.store._conn.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"]  # noqa: SLF001
        out = await host.uninstall("echo-plugin")
        assert out["dropped"]["channel"] == 1 and out["dropped"]["threads"] == 1
        assert h.runtime.store.channel_by_name("echo-plugin") is None
        assert h.runtime.store.plugin_get("echo-plugin") is None
        sessions_after = h.runtime.store._conn.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"]  # noqa: SLF001
        assert sessions_after == sessions_before, "解绑只动通道与绑定两张表，会话不受影响"

"""插件宿主（CHANNEL_PLUGIN_SPEC §3.1–§3.4）。

判据：扫描只读清单不跑代码；启用要握手成功才算运行；停用不留孤儿；崩溃不重启风暴；
子进程环境不继承核心凭据；卸载保留核心历史。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from conftest import open_mgmt, running_core
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


# ---------- 分发渠道（§七）：分发包安装 ----------


def _make_archive(tmp_path, *, plugin_id: str = "zip-plugin", wrapper: str = "") -> Path:
    """打一个可用的插件分发包（zip）；wrapper 非空 = 内容放进一层目录（两种放法都要认）。"""
    archive = tmp_path / f"{plugin_id}.zip"
    manifest = json.dumps(
        {"id": plugin_id, "name": f"{plugin_id} 插件", "version": "0.1.0", "ump": "1.x",
         "entry": ["python", "main.py"], "description": "测试用", "author": "测试"},
        ensure_ascii=False,
    )
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{wrapper}manifest.json", manifest)
        zf.writestr(f"{wrapper}main.py", _stub("echo.py"))
    return archive


async def test_install_from_archive_then_enable(tmp_path) -> None:
    """装 ≠ 启用：装完是 installed（没跑过插件代码），启用才握手；走真管理面。"""
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        archive = _make_archive(tmp_path, wrapper="zip-plugin-0.1.0/")  # 单层包装目录也要认
        out = await mgmt.call("plugin.install", archive=str(archive))
        assert out["installed"] == "zip-plugin" and out["state"] == "installed" and out["enabled"] is False
        assert (tmp_path / "plugins" / "zip-plugin" / "main.py").is_file()

        row = h.runtime.store.plugin_get("zip-plugin")
        assert str(row["state"]) == "installed" and not int(row["enabled"]), row
        enabled = await mgmt.call("plugin.enable", id="zip-plugin", timeout=30.0)
        assert enabled["enable"]["enabled"] is True, enabled
        assert str(h.runtime.store.plugin_get("zip-plugin")["state"]) == "running"


async def test_install_refuses_unsafe_archive(tmp_path) -> None:
    """越界路径条目：整包拒收，一个字节都不落盘（zip 炸弹 / 穿越闸）。"""
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"id": "evil", "name": "坏", "version": "1",
                                                     "entry": ["python", "main.py"]}, ensure_ascii=False))
            zf.writestr("../escaped.txt", "nope")
        with pytest.raises(Exception) as exc:
            await mgmt.call("plugin.install", archive=str(archive))
        assert "越界" in str(exc.value), exc.value
        assert not (tmp_path / "escaped.txt").exists()
        assert not (tmp_path / "plugins" / "evil").exists()


async def test_install_refuses_bad_manifest_and_existing_dir(tmp_path) -> None:
    """清单不合规整包拒收；同名目录已存在要显式 replace（替换=整目录换掉）。"""
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"id": "bad-plugin", "name": "坏", "version": "1"},
                                                    ensure_ascii=False))
        with pytest.raises(Exception) as exc:
            await mgmt.call("plugin.install", archive=str(bad))
        assert "清单不合规" in str(exc.value), exc.value
        assert not (tmp_path / "plugins" / "bad-plugin").exists()

        good = _make_archive(tmp_path, plugin_id="dup-plugin")
        await mgmt.call("plugin.install", archive=str(good))
        with pytest.raises(Exception) as exc:
            await mgmt.call("plugin.install", archive=str(good))
        assert "已存在" in str(exc.value), exc.value
        again = await mgmt.call("plugin.install", archive=str(good), replace=True)
        assert again["state"] == "installed"

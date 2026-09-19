"""设置面行为：读取打码、写入生效、非法保留原值（DESKTOP_SPEC §3.3）。"""

from __future__ import annotations

import json

import pytest
import yaml

from conftest import open_mgmt, running_core
from isekai_core import ump
from isekai_core.ump import UmpError

RAW_KEY = "sk-test-abcdefghijklmn"


def write_config(tmp_path, api_key: str = RAW_KEY) -> None:
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "llm": {"base_url": "https://api.example.com", "model": "m-1", "api_key": api_key},
                "placeholder": {"character_id": "ph-x"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def read_config(tmp_path) -> dict:
    return yaml.safe_load((tmp_path / "config" / "config.yaml").read_text(encoding="utf-8"))


async def test_settings_get_masks_api_key(tmp_path):
    write_config(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            result = await mgmt.call("settings.get")
            assert result["llm"]["base_url"] == "https://api.example.com"
            assert result["llm"]["model"] == "m-1"
            assert result["llm"]["api_key_set"] is True
            dumped = json.dumps(result, ensure_ascii=False)
            assert RAW_KEY not in dumped
            assert result["llm"]["api_key"].endswith("klmn")
            assert result["core"]["config_file"].endswith("config.yaml")
        finally:
            await mgmt.close()


async def test_settings_set_writes_config_and_applies_to_live_client(tmp_path):
    write_config(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            result = await mgmt.call("settings.set", llm={"model": "m-2", "api_key": "sk-new-9999"})
            assert result["llm"]["model"] == "m-2"
            assert "sk-new-9999" not in json.dumps(result, ensure_ascii=False)

            on_disk = read_config(tmp_path)
            assert on_disk["llm"]["model"] == "m-2"
            assert on_disk["llm"]["api_key"] == "sk-new-9999"
            assert on_disk["placeholder"]["character_id"] == "ph-x"  # 其它段保留

            assert h.runtime.service.llm.cfg.model == "m-2"  # 即时生效
            assert h.runtime.service.cfg.llm.api_key == "sk-new-9999"
        finally:
            await mgmt.close()


async def test_settings_set_rejects_invalid_and_keeps_original(tmp_path):
    write_config(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            with pytest.raises(UmpError) as blank:
                await mgmt.call("settings.set", llm={"model": "   "})
            assert blank.value.code == ump.Err.PROTOCOL
            with pytest.raises(UmpError):
                await mgmt.call("settings.set", llm={"nope": 1})
            with pytest.raises(UmpError):
                await mgmt.call("settings.set", llm={"temperature": 9})

            assert read_config(tmp_path)["llm"]["model"] == "m-1"
            assert h.runtime.service.cfg.llm.model == "m-1"
            assert h.runtime.service.cfg.llm.api_key == RAW_KEY
        finally:
            await mgmt.close()


async def test_missing_api_key_is_reported_not_faked(tmp_path):
    async with running_core(tmp_path) as h:  # 无 config.yaml：默认空 Key
        mgmt = await open_mgmt(h)
        try:
            result = await mgmt.call("settings.get")
            assert result["llm"]["api_key_set"] is False
            assert result["llm"]["api_key"] == ""
        finally:
            await mgmt.close()

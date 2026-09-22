#!/usr/bin/env python3
"""UI 对比度检查：按 desktop/src/user/user.css 的真实令牌值算 WCAG 比值。

用法：.venv/Scripts/python.exe scripts/_check_ui_contrast.py
退出码 0 = 全部达标；1 = 有不达标项（逐条列出场景与比值）。

口径（USER_INTERFACE_DESIGN §10.2）：正文 / 状态文字 ≥ 4.5:1；焦点描边与必要控件边界 ≥ 3:1。
浅色 / 深色（显式 data-theme="dark"）/ 深色（跟随系统 prefers-color-scheme）各算一份。
改完调色板直接复跑；新增配对写进 CASES。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "desktop" / "src" / "user" / "user.css"


def tokens(text: str, selector: str) -> dict[str, str]:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", text, re.S)
    if not match:
        return {}
    return {
        name: value
        for name, value in re.findall(r"--u-([a-z-]+)\s*:\s*(#[0-9a-fA-F]{6})", match.group(1))
    }


def input_border_token(text: str) -> str | None:
    match = re.search(r"\.u-input\s*,\s*\.u-textarea\s*\{([^}]*)\}", text, re.S)
    if not match:
        return None
    hit = re.search(r"border[^;]*var\(--u-([a-z-]+)\)", match.group(1))
    return hit.group(1) if hit else None


def to_linear(channel: float) -> float:
    channel = channel / 255.0
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * to_linear(r) + 0.7152 * to_linear(g) + 0.0722 * to_linear(b)


def ratio(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def build_cases(border: str | None) -> list[tuple[str, str, str, str, float]]:
    cases: list[tuple[str, str, str, str, float]] = []
    for theme in ("浅色", "深色（显式）", "深色（跟随系统）"):
        for kind in ("ok", "bad", "warn"):
            for bg in ("surface", "sunken", "bg"):
                cases.append((theme, f"状态 {kind} / {bg}", kind, bg, 4.5))
        cases.append((theme, "正文 muted / bg", "muted", "bg", 4.5))
        cases.append((theme, "焦点描边 fg / bg", "fg", "bg", 3.0))
        if border:
            for bg in ("bg", "surface", "sunken"):
                cases.append((theme, f"控件边界 {border} / {bg}", border, bg, 3.0))
    return cases


def main() -> int:
    text = CSS.read_text(encoding="utf-8")
    base = tokens(text, ":root")
    dark = {**base, **tokens(text, 'html[data-theme="dark"]')}
    media_match = re.search(
        r"@media \(prefers-color-scheme: dark\)\s*\{(.*?)\n\}", text, re.S
    )
    system = {**base, **tokens(media_match.group(1), 'html[data-theme="system"]')} if media_match else dark
    palettes = {"浅色": base, "深色（显式）": dark, "深色（跟随系统）": system}

    border = input_border_token(text)
    if border is None:
        print("警告：没找到 .u-input 的边框令牌，跳过控件边界检查")
    cases = build_cases(border)

    failures = 0
    print(f"# 对比度检查：{CSS}")
    for theme, name, fg_key, bg_key, need in cases:
        palette = palettes[theme]
        fg, bg = palette.get(fg_key), palette.get(bg_key)
        if not fg or not bg:
            print(f"[?] {theme} {name}: 缺令牌 {fg_key}={fg} / {bg_key}={bg}")
            failures += 1
            continue
        value = ratio(fg, bg)
        ok = value >= need
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {theme:<10} {name:<28} {value:5.2f}:1 （要求 {need}）  {fg} on {bg}")

    print(f"\n{'全部达标' if failures == 0 else f'{failures} 项不达标'}（共 {len(cases)} 项）")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
check_env_sync.py — 依赖声明漂移检查（pixi.toml  ↔  v5.0/requirements.txt）
=============================================================================
为什么要这个: 同一个项目的依赖曾经存在两份声明（一份给 pixi、一份给 pip/uv），
两份各自演化 → 新机器装出来的环境与老机器不同，而没有任何东西会报错。
这个脚本把"两份必须一致"变成一条可执行的断言。

规则:
  - 两边都要声明**同一组包、同一组版本**；名字归一化（大小写/`-`↔`_`）后比较。
  - pixi 侧只比 [pypi-dependencies]（conda 侧如 python 不在 requirements.txt 里）。
  - extras 只做提示，不参与相等性（requirements.txt 用 `pkg[extra]` 表达，pixi 用
    { extras = [...] } 表达；两者都表达同一件事时即算一致）。
  - 任一边多/少一个包，或版本号不同 → 退出码 1 并列出差异。

用法: python check_env_sync.py        (或 pixi run check-deps)
      零第三方依赖，Python 3.11+ 即可（tomllib 走标准库）。
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PIXI_TOML = ROOT / "pixi.toml"
REQUIREMENTS = ROOT / "v5.0" / "requirements.txt"

# 归档研究脚本专用，只在 pixi 的 research 环境里，requirements.txt 不管这些
IGNORE_IN_PIXI = {"matplotlib", "scipy"}


def norm(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def parse_pixi(path: Path) -> dict[str, str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for name, spec in (data.get("pypi-dependencies") or {}).items():
        if norm(name) in IGNORE_IN_PIXI:
            continue
        out[norm(name)] = spec["version"] if isinstance(spec, dict) else str(spec)
    return out


REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(\[[^\]]*\])?\s*([<>=!~][^;#\s]*)?\s*(#.*)?$")


def parse_requirements(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = REQ_RE.match(line)
        if not m:
            print(f"!! requirements.txt 有一行无法解析: {raw!r}")
            sys.exit(2)
        name, _extras, version = m.group(1), m.group(2), m.group(3) or ""
        out[norm(name)] = version.replace(" ", "")
    return out


def main() -> int:
    pixi = parse_pixi(PIXI_TOML)
    req = parse_requirements(REQUIREMENTS)

    only_pixi = sorted(set(pixi) - set(req))
    only_req = sorted(set(req) - set(pixi))
    ver_diff = sorted(k for k in set(pixi) & set(req) if pixi[k] != req[k])

    if not (only_pixi or only_req or ver_diff):
        print(f"✔ 依赖声明一致 — {len(pixi)} 个包，pixi.toml ↔ v5.0/requirements.txt 同版")
        for k in sorted(pixi):
            print(f"    {k:<20} {pixi[k]}")
        return 0

    print("!! 依赖声明漂移 —— 修掉再提交")
    for k in only_pixi:
        print(f"    只在 pixi.toml: {k} {pixi[k]}")
    for k in only_req:
        print(f"    只在 requirements.txt: {k} {req[k]}")
    for k in ver_diff:
        print(f"    版本不同: {k}  pixi={pixi[k]}  requirements={req[k]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

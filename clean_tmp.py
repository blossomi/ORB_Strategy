#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_tmp.py — 清理测试/验证跑出来的临时产物 (白名单, 默认 dry-run)
====================================================================
白名单直接对齐 .gitignore 里**已声明为可丢弃**的那几类, 不自己发明规则:

  pycache   __pycache__/ 目录树 + 散落的 *.pyc   Python 字节码, 自动重建
  dsstore   *.DS_Store                          macOS 垃圾
  verify    _verify_{orig,cand}_*.csv           verify_live.py 每个 scope 的滑点落盘
            _verify_live_*.csv                  archive/live 旧验证脚本的同类产物
  vlog      _verify_*.log                       验证脚本的运行日志
  ostmp     $TMPDIR/slip_selftest.csv           slippage_tracker.py 自测产物

⚠️ **刻意不碰**的东西 (每条都是删了会疼的):
  - live 审计底稿: `v5.0/live_slippage*.csv` / `v5.0/logs/` / `archive/live/logs/`
    —— 真单滑点采集是交付物 (README §6 已知风险②靠它校准), 删了不可恢复
  - 环境与包缓存: `.pixi/ .venv/ .uv_cache/ .uv_pkg_cache/`
    —— 重建代价高 (百 MB~GB), 且不是测试产物
  - 数据与结果: `**/data/` `**/results/`
    —— `*.parquet` 不进库 (删了要从别处重拷); `results/*.csv` 是 parity 的候选面
  - **任何 `.py` 源文件** —— `archive/**/_verify_*.py` 是**源码**不是产物,
    所以本脚本只按 `_verify_*.csv` / `_verify_*.log` 这类**输出后缀**匹配

用法 (纯 stdlib, 系统 python3 即可, 不需要 pixi 环境):
  python3 clean_tmp.py           # dry-run: 只报告要删什么 (默认, 安全)
  python3 clean_tmp.py --yes     # 真删
  pixi run clean-check           # = dry-run
  pixi run clean                 # = --yes (任务见 pixi.toml)
"""
import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 锚定脚本所在目录 —— 不用 cwd (本仓库踩过 cwd 相对路径的坑, 见 root README)
ROOT = Path(__file__).resolve().parent

# 整棵树都不进 (环境/缓存/版本库): 省时间, 更重要的是别误删
SKIP_TREES = {".git", ".pixi", ".venv", ".uv_cache", ".uv_pkg_cache",
              "node_modules", "_import_staging"}

# 保护前缀 (相对 ROOT, POSIX 风格) —— 命中即跳过, 无论哪个白名单匹配到
PROTECTED_PREFIXES = ("data/", "results/", "logs/")
PROTECTED_NAMES = ("live_slippage.csv",)

# 白名单: (组名, 匹配函数)。匹配函数吃相对 posix 路径 + Path, 返回 bool
GLOBS = {
    "verify": ("_verify_orig_*.csv", "_verify_cand_*.csv", "_verify_live_*.csv"),
    "vlog": ("_verify_*.log",),
    "dsstore": (".DS_Store", "*.DS_Store"),
}


def _is_protected(rel: str) -> bool:
    """保护名单终检 —— 白名单之外的第二道闸。"""
    if Path(rel).name in PROTECTED_NAMES:
        return True
    return any(rel.startswith(p) or f"/{p}" in f"/{rel}"
               for p in PROTECTED_PREFIXES)


def collect() -> dict[str, list[Path]]:
    """扫仓库 + OS 临时目录, 按组返回待删路径 (只收集, 不删)。"""
    found: dict[str, list[Path]] = {k: [] for k in ("pycache", "dsstore",
                                                    "verify", "vlog", "ostmp")}
    patterns = {g: [Path(p) for p in pats] for g, pats in GLOBS.items()}

    for dirpath, dirnames, filenames in os.walk(ROOT, topdown=True):
        # 剪枝: 环境/缓存/版本库, 以及即将整棵删掉的 __pycache__
        pyc_dirs = [d for d in dirnames if d == "__pycache__"]
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_TREES and d != "__pycache__"]
        for d in pyc_dirs:
            p = Path(dirpath) / d
            found["pycache"].append(p)

        for fn in filenames:
            p = Path(dirpath) / fn
            rel = p.relative_to(ROOT).as_posix()
            if _is_protected(rel):
                continue
            if fn.endswith(".pyc"):                  # 散落字节码 (少见但要收)
                found["pycache"].append(p)
                continue
            for group, pats in patterns.items():
                if any(p.match(str(pat)) for pat in pats):
                    found[group].append(p)
                    break

    # 自测产物落在系统临时目录 (仓库外, 单独处理)
    tmp = Path(tempfile.gettempdir()) / "slip_selftest.csv"
    if tmp.exists():
        found["ostmp"].append(tmp)
    return found


def _size(p: Path) -> int:
    """文件大小; 目录则递归求和 (坏了就按 0 算, 不影响清理)。"""
    try:
        if p.is_file():
            return p.stat().st_size
        total = 0
        for dirpath, _, files in os.walk(p):
            for f in files:
                try:
                    total += (Path(dirpath) / f).stat().st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


LABELS = {
    "pycache": "Python 字节码 (__pycache__/ + *.pyc)",
    "dsstore": "macOS .DS_Store",
    "verify": "验证脚本滑点 CSV (_verify_*.csv)",
    "vlog": "验证脚本日志 (_verify_*.log)",
    "ostmp": "系统临时目录自测产物 (slip_selftest.csv)",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="清理测试临时产物 (白名单)")
    ap.add_argument("--yes", action="store_true",
                    help="真删 (默认只 dry-run 报告); `pixi run clean` 隐含此项")
    args = ap.parse_args()

    found = collect()
    total_n = sum(len(v) for v in found.values())
    total_b = sum(_size(p) for v in found.values() for p in v)

    print(f"扫描根目录: {ROOT}")
    if total_n == 0:
        print("\n✔ 没有需要清理的临时文件 (已是干净态)")
        return 0

    print()
    for group, paths in found.items():
        if not paths:
            continue
        size = sum(_size(p) for p in paths)
        print(f"  [{group}] {LABELS[group]}")
        print(f"          {len(paths)} 项 / {_human(size)}")
        for p in sorted(paths)[:8]:
            try:
                shown = p.relative_to(ROOT).as_posix()
            except ValueError:
                shown = str(p)                # OS 临时目录, 不在仓库内
            print(f"            {shown}")
        if len(paths) > 8:
            print(f"            … 另有 {len(paths) - 8} 项")
    print(f"\n合计 {total_n} 项 / {_human(total_b)}")

    print("\n保留 (刻意不碰): live 滑点与运行日志 / 环境与包缓存 / data / results / 全部 .py")
    if not args.yes:
        print("\n-- dry-run (未删)。真删: python3 clean_tmp.py --yes   或   pixi run clean")
        return 0

    removed = failed = 0
    for paths in found.values():
        for p in paths:
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                removed += 1
            except OSError as e:
                failed += 1
                print(f"  !! 删除失败 {p}: {e}", file=sys.stderr)
    print(f"\n✔ 已删除 {removed} 项 / {_human(total_b)}"
          + (f"; {failed} 项失败" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

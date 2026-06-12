#!/usr/bin/env python3
"""PathPlan — 全资源路径管理的统一数据层。

包装 LeanConfig 并扩展 MCP、benchmark、缓存、输出路径。

用法:
    >>> from omega.plan.path_plan import PathPlan, resolve_path_plan
    >>> plan = resolve_path_plan()
    >>> plan.lean_bin
    PosixPath('/home/shenli/.elan/toolchains/4.30.0/bin/lean')
    >>> plan.mcp_server
    'http://localhost:8100'
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omega.resource.lean_config import LeanConfig, load_lean_config

logger = logging.getLogger("omega.plan.path_plan")

# ── 默认路径 ─────────────────────────────────────────────────

_DEFAULT_PROJECT = Path.home() / "lean-paper-plane"
_DEFAULT_LEAN_VERSION = "4.30.0"
_DEFAULT_TOOLCHAIN = Path.home() / ".elan" / "toolchains" / _DEFAULT_LEAN_VERSION
_DEFAULT_BENCHMARK_ROOT = Path.home() / "Gitlab" / "Agentic4Sci" / "omega-architect" / "benchmarks" / "minif2f"
_DEFAULT_CACHE_ROOT = Path.home() / ".omega" / "cache"
_DEFAULT_OUTPUT_ROOT = Path.home() / ".omega" / "experiments"


@dataclass
class PathPlan:
    """全资源路径计划 — 一次解析，多处使用。

    继承 LeanConfig 的全量字段，并增加 Omega 运行所需的
    其他资源路径。MCP 服务器地址非文件路径但也在此管理。
    """

    # ── Lean 工具链（来自 LeanConfig） ──
    version: str = _DEFAULT_LEAN_VERSION
    project_path: Path = _DEFAULT_PROJECT
    lean_bin: Path = _DEFAULT_TOOLCHAIN / "bin" / "lean"
    lake_bin: Path = _DEFAULT_TOOLCHAIN / "bin" / "lake"
    olean_count: int = 0
    mathlib_size_gb: float = 0.0
    compile_channel: str = "lake_env"
    compile_fallback: list[str] = field(default_factory=lambda: ["bare_lean"])

    # ── MCP 服务 ──
    mcp_server: str = "http://localhost:8100"          # lean-lsp-mcp 地址

    # ── 基准测试 ──
    benchmark_root: Path = _DEFAULT_BENCHMARK_ROOT     # MiniF2F 基准路径

    # ── 缓存 & 输出 ──
    cache_root: Path = _DEFAULT_CACHE_ROOT              # 对话/证明缓存
    output_root: Path = _DEFAULT_OUTPUT_ROOT             # 实验结果输出

    # ── 元数据 ──
    _source: str = ""                                   # 配置来源（同 LeanConfig）

    # ── 工厂 ──────────────────────────────────────────────────

    @classmethod
    def from_lean_config(cls, lc: LeanConfig, **overrides) -> PathPlan:
        """从 LeanConfig 构建，覆盖指定字段。"""
        return cls(
            version=lc.version,
            project_path=lc.project_path,
            lean_bin=lc.lean_bin,
            lake_bin=lc.lake_bin,
            olean_count=lc.olean_count,
            mathlib_size_gb=lc.mathlib_size_gb,
            compile_channel=lc.compile_channel,
            compile_fallback=list(lc.compile_fallback),
            _source=lc._source,
            **overrides,
        )

    # ── 健康检查 ──────────────────────────────────────────────

    def project_exists(self) -> bool:
        return self.project_path.is_dir()

    def binaries_ok(self) -> bool:
        return self.lean_bin.is_file() and self.lake_bin.is_file()

    def mathlib_ok(self) -> int:
        """检查 Mathlib 缓存健康。返回 olean 数。"""
        if not self.project_exists():
            return 0
        return self.olean_count

    def benchmark_exists(self) -> bool:
        """MiniF2F 基准目录是否存在。"""
        return self.benchmark_root.is_dir()

    def mcp_ok(self) -> bool:
        """MCP 服务器地址非空。健康检查需运行时确认。"""
        return bool(self.mcp_server)

    # ── 序列化 ──────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "lean": {
                "version": self.version,
                "project_path": str(self.project_path),
                "binary": {"lean": str(self.lean_bin), "lake": str(self.lake_bin)},
                "mathlib": {"olean_count": self.olean_count, "size_gb": self.mathlib_size_gb},
                "compile": {"channel": self.compile_channel, "fallback": list(self.compile_fallback)},
            },
            "mcp": {"server": self.mcp_server},
            "benchmark": {"root": str(self.benchmark_root)},
            "cache": {"root": str(self.cache_root)},
            "output": {"root": str(self.output_root)},
        }

    def summary(self) -> str:
        ok = "✅" if self.binaries_ok() else "❌"
        ml = "✅" if self.mathlib_ok() else "❌"
        bm = "✅" if self.benchmark_exists() else "❌"
        return (
            f"Lean {self.version} {ok} | Mathlib {ml} ({self.olean_count} oleans) | "
            f"Bench {bm} | Cache {self.cache_root}"
        )


# ── 解析入口 ─────────────────────────────────────────────────


def resolve_path_plan(**overrides) -> PathPlan:
    """加载 LeanConfig → 包装为 PathPlan → 合并 overrides。

    resolve 命名表示该计划可被多次解析（无副作用缓存），
    但通常每轮 `inner_loop()` 调用前解析一次即可。
    """
    le = load_lean_config()
    plan = PathPlan.from_lean_config(le, **overrides)
    logger.info("PathPlan resolved: %s", plan.summary())
    return plan

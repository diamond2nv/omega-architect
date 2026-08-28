#!/usr/bin/env python3
"""PersistentCompileShell — 持久 lake env shell 会话。

避免每次编译重新 spawn ``lake env``（~100ms 进程启动开销）。

设计
----
- 启动一个持久 bash 进程，source lake env
- 每次编译写入临时 .lean 文件，通过持久 shell 调用 lean
- 多个编译共用一个 shell 环境（cwd、env vars、OS 缓存）

与 CompileGate 的关系
--------------------
CompileGate.compile() 内部自动使用 PersistentCompileShell。
用户无需感知此层，直接调 CompileGate.compile(code) 即可。

用法
----
    shell = PersistentCompileShell(project_dir="<project-dir>")
    result = shell.compile("theorem t : 1 + 1 = 2 := by\\n  norm_num")
    print(result.success)  # True
    shell.close()
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omega.resource.lean_config import load_lean_config


@dataclass
class CompileOutput:
    """一次编译的输出。"""
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    elapsed_ms: int


def _resolve_lean_command() -> list[str]:
    """返回可直接调用的 lean 命令（已解析 lake env）。

    注意：非持久模式时用 ``lake env lean --stdin``。
    持久模式创建 bash 后在 shell 内部 source lake env，此处只返回 lean 路径。
    """
    cfg = load_lean_config()
    return [str(cfg.lean_bin)]


class PersistentCompileShell:
    """持久编译外壳。

    通过持久 bash 进程 + 临时文件避免每次 spawn lake env 的开销。
    仅在批量编译（50+次）场景下有价值，单次编译直接用 subprocess.run。
    """

    def __init__(self, project_dir: str | Path | None = None, timeout: int = 60):
        cfg = load_lean_config()
        self._project_dir = Path(project_dir) if project_dir else cfg.project_path
        self._timeout = timeout
        self._lean_bin = str(cfg.lean_bin)
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._proc: subprocess.Popen | None = None
        self._started = False

    def start(self) -> None:
        """启动持久 bash 进程并 source lake env。"""
        if self._started:
            return

        if not self._project_dir.exists():
            raise FileNotFoundError(f"Lean project not found: {self._project_dir}")

        # 临时目录存放 .lean 文件
        self._tmpdir = tempfile.TemporaryDirectory(prefix="omega_compile_")
        self._workdir = Path(self._tmpdir.name)

        # 启动 bash + source lake env
        # 使用 --noediting 禁用 readline（避免交互式陷阱）
        self._proc = subprocess.Popen(
            ["bash", "--noediting", "+H"],  # +H 禁用历史展开
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self._project_dir),
            text=True,
            bufsize=0,  # unbuffered
        )

        # source lake env
        self._exec("source <(lake env) 2>/dev/null; echo 'LAKE_READY'")
        ready = self._read_until("LAKE_READY", timeout=30)
        if "LAKE_READY" not in ready:
            raise RuntimeError(
                f"PersistentCompileShell failed to source lake env. "
                f"Output: {ready[:200]}"
            )

        self._started = True

    # ── 内部：shell 交互 ────────────────────────────────────

    def _exec(self, cmd: str) -> None:
        """向持久 shell 写入命令。"""
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(cmd + "\n")
        self._proc.stdin.flush()

    def _read_until(self, marker: str, timeout: float = 10.0) -> str:
        """从 stdout 读取直到出现 marker。"""
        assert self._proc is not None and self._proc.stdout is not None
        t0 = time.time()
        out_parts: list[str] = []
        while time.time() - t0 < timeout:
            line = self._proc.stdout.readline()
            if not line:
                break
            out_parts.append(line)
            if marker in line:
                break
        return "".join(out_parts)

    # ── 公开接口 ────────────────────────────────────────────

    def compile(self, code: str) -> dict[str, Any]:
        """编译一段 Lean 代码。

        将代码写入临时 .lean 文件，在持久 shell 中调用 lean 编译。
        返回格式与 ``real_compile_callback()`` 兼容。
        """
        if not self._started:
            self.start()

        t0 = time.perf_counter()

        # 写入临时文件
        assert self._workdir is not None
        src_path = self._workdir / "input.lean"
        src_path.write_text(code, encoding="utf-8")

        # 在持久 shell 中编译：lean input.lean 2>&1; echo 'COMPILE_DONE'
        cmd = (
            f"{self._lean_bin} {src_path} 2>&1; "
            f"echo EXIT_CODE=$?"
        )
        self._exec(cmd)

        # 读取输出直到 EXIT_CODE=N
        output = self._read_until("EXIT_CODE=", timeout=self._timeout)
        elapsed = int((time.perf_counter() - t0) * 1000)

        # 解析 EXIT_CODE
        exit_code = -1
        for line in output.split("\n"):
            if line.startswith("EXIT_CODE="):
                with contextlib.suppress(IndexError, ValueError):
                    exit_code = int(line.split("=")[1])
                break

        # 分离 stdout 和 stderr（从输出中移除标记行）
        clean_output = "\n".join(
            line for line in output.split("\n")
            if not line.startswith("EXIT_CODE=")
        )

        # 用 t2_real 的诊断解析器解析输出
        from omega.verify.t2_real import parse_lean_diagnostics
        diagnostics = parse_lean_diagnostics(clean_output)

        return {
            "diagnostics": diagnostics,
            "exit_code": exit_code,
            "stdout": clean_output[:500] if clean_output else "",
            "elapsed_ms": elapsed,
        }

    # ── 生命周期 ────────────────────────────────────────────

    def close(self) -> None:
        """关闭持久 shell 并清理临时目录。"""
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None
        if self._tmpdir:
            with contextlib.suppress(Exception):
                self._tmpdir.cleanup()
            self._tmpdir = None
        self._started = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

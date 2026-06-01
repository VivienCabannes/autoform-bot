"""LaTeX execution — compilation and log parsing. No MCP dependencies."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

logger = getLogger(__name__)

DEFAULT_VERSION_CHECK_TIMEOUT = 10


@dataclass(frozen=True)
class LatexConfig:
    """Configuration for a LaTeX executor instance."""

    # Working directory for compilation (temp dir used if empty)
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)

    # LaTeX engine: "pdflatex", "xelatex", "lualatex", or "latexmk"
    engine: str = "pdflatex"

    # Extra CLI flags passed to the engine
    extra_args: list[str] = field(default_factory=list)

    # Timeout for a single compilation run
    timeout: float = 120.0

    # Number of compilation passes (needed for TOC / references)
    num_passes: int = 1

    # Whether to clean auxiliary files after compilation
    clean_aux: bool = True


class LatexExecutor:
    """LaTeX compilation manager.

    Compiles LaTeX source strings or files to PDF, capturing
    stdout/stderr and returning structured results.
    """

    # Auxiliary file extensions that are cleaned after compilation.
    AUX_EXTENSIONS: tuple[str, ...] = (
        ".aux",
        ".log",
        ".out",
        ".toc",
        ".lof",
        ".lot",
        ".fls",
        ".fdb_latexmk",
        ".synctex.gz",
        ".bbl",
        ".blg",
        ".nav",
        ".snm",
        ".vrb",
    )

    def __init__(self, config: LatexConfig | None = None) -> None:
        self.config = config or LatexConfig()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compile_source(
        self,
        source: str,
        *,
        filename: str = "document.tex",
        timeout: float | None = None,
        num_passes: int | None = None,
    ) -> dict[str, Any]:
        """Compile a LaTeX source string and return structured results.

        The source is written to a temporary directory, compiled, and
        the results are returned as a dict.

        Args:
            source: Full LaTeX source (must include \\documentclass … \\end{document}).
            filename: Name for the .tex file inside the temp directory.
            timeout: Compilation timeout in seconds (overrides config default).
            num_passes: Number of compilation passes (overrides config default).

        Returns:
            Dict with keys: success, pdf_path, log, errors, warnings.
        """
        timeout = timeout or self.config.timeout
        num_passes = num_passes if num_passes is not None else self.config.num_passes

        work_dir = self.config.cwd or tempfile.mkdtemp(prefix="latex_exec_")
        tex_path = os.path.join(work_dir, filename)

        try:
            with open(tex_path, "w", encoding="utf-8") as f:
                f.write(source)

            return self._compile(
                tex_path,
                work_dir=work_dir,
                timeout=timeout,
                num_passes=num_passes,
            )
        finally:
            # If we created a temp dir and the caller didn't supply cwd,
            # keep it around (the result includes pdf_path).
            pass

    def compile_file(
        self,
        tex_path: str,
        *,
        timeout: float | None = None,
        num_passes: int | None = None,
    ) -> dict[str, Any]:
        """Compile an existing .tex file and return structured results.

        Args:
            tex_path: Absolute or relative path to a .tex file.
            timeout: Compilation timeout in seconds.
            num_passes: Number of compilation passes.

        Returns:
            Dict with keys: success, pdf_path, log, errors, warnings.
        """
        timeout = timeout or self.config.timeout
        num_passes = num_passes if num_passes is not None else self.config.num_passes
        work_dir = os.path.dirname(os.path.abspath(tex_path))

        return self._compile(
            os.path.abspath(tex_path),
            work_dir=work_dir,
            timeout=timeout,
            num_passes=num_passes,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_command(self, tex_path: str) -> list[str]:
        """Build the CLI command list for the configured engine."""
        engine = self.config.engine

        if engine == "latexmk":
            cmd = [
                "latexmk",
                "-pdf",
                "-interaction=nonstopmode",
                "-halt-on-error",
                *self.config.extra_args,
                tex_path,
            ]
        else:
            cmd = [
                engine,
                "-interaction=nonstopmode",
                "-halt-on-error",
                *self.config.extra_args,
                tex_path,
            ]

        return cmd

    def _compile(
        self,
        tex_path: str,
        *,
        work_dir: str,
        timeout: float,
        num_passes: int,
    ) -> dict[str, Any]:
        """Run the LaTeX engine and collect results."""
        cmd = self._build_command(tex_path)
        env = os.environ.copy()
        env.update(self.config.env)

        all_stdout = ""
        all_stderr = ""
        returncode: int | None = None

        with self._lock:
            for pass_num in range(1, num_passes + 1):
                logger.debug(
                    "LaTeX pass %d/%d: %s",
                    pass_num,
                    num_passes,
                    " ".join(cmd),
                )
                try:
                    result = subprocess.run(
                        cmd,
                        cwd=work_dir,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        env=env,
                    )
                    all_stdout += result.stdout
                    all_stderr += result.stderr
                    returncode = result.returncode
                except subprocess.TimeoutExpired:
                    return {
                        "success": False,
                        "pdf_path": None,
                        "log": all_stdout,
                        "errors": [f"Compilation timed out after {timeout}s on pass {pass_num}"],
                        "warnings": [],
                    }
                except FileNotFoundError:
                    return {
                        "success": False,
                        "pdf_path": None,
                        "log": "",
                        "errors": [
                            f"LaTeX engine '{self.config.engine}' not found. Make sure it is installed and on PATH."
                        ],
                        "warnings": [],
                    }

        # Determine PDF path
        stem = Path(tex_path).stem
        pdf_path = os.path.join(work_dir, f"{stem}.pdf")
        pdf_exists = os.path.isfile(pdf_path)

        # Parse log for errors and warnings
        log_path = os.path.join(work_dir, f"{stem}.log")
        log_content = ""
        if os.path.isfile(log_path):
            with open(log_path, encoding="utf-8", errors="replace") as f:
                log_content = f.read()

        errors = self._extract_errors(log_content)
        warnings = self._extract_warnings(log_content)

        success = returncode == 0 and pdf_exists

        # Clean aux files if requested
        if self.config.clean_aux:
            self._clean_aux(work_dir, stem)

        return {
            "success": success,
            "pdf_path": pdf_path if pdf_exists else None,
            "log": log_content or all_stdout,
            "errors": errors,
            "warnings": warnings,
        }

    @staticmethod
    def _extract_errors(log: str) -> list[str]:
        """Extract error lines from a LaTeX log."""
        errors: list[str] = []
        for line in log.splitlines():
            if line.startswith("!"):
                errors.append(line)
        return errors

    @staticmethod
    def _extract_warnings(log: str) -> list[str]:
        """Extract warning lines from a LaTeX log."""
        warnings: list[str] = []
        for line in log.splitlines():
            lower = line.lower()
            if "warning" in lower and not line.startswith("!"):
                warnings.append(line.strip())
        return warnings

    def _clean_aux(self, work_dir: str, stem: str) -> None:
        """Remove auxiliary files produced during compilation."""
        for ext in self.AUX_EXTENSIONS:
            path = os.path.join(work_dir, f"{stem}{ext}")
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass

    def check_engine(self) -> dict[str, Any]:
        """Check whether the configured LaTeX engine is available."""
        engine = self.config.engine
        binary = engine if engine != "latexmk" else "latexmk"
        found = shutil.which(binary) is not None

        version = ""
        if found:
            try:
                result = subprocess.run(
                    [binary, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=DEFAULT_VERSION_CHECK_TIMEOUT,
                )
                version = result.stdout.splitlines()[0] if result.stdout else ""
            except Exception:
                pass

        return {
            "engine": engine,
            "available": found,
            "version": version,
        }

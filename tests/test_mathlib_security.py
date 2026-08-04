from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from servers.mathlib import core
from servers.mathlib.core import grep_mathlib, read_mathlib_file


def _fake_mathlib(tmp_path: Path) -> Path:
    root = tmp_path / "mathlib"
    source = root / "Mathlib"
    source.mkdir(parents=True)
    (source / "Safe.lean").write_text(
        "theorem safe : True := trivial\n", encoding="utf-8"
    )
    (source / "notes.txt").write_text("not Lean\n", encoding="utf-8")
    return root


def test_mathlib_read_is_confined_to_lean_source_root(tmp_path: Path):
    root = _fake_mathlib(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    assert "theorem safe" in read_mathlib_file(root, "Mathlib/Safe.lean")
    assert "escapes" in read_mathlib_file(root, "../secret.txt")
    assert "only Mathlib .lean" in read_mathlib_file(root, "Mathlib/notes.txt")


def test_mathlib_search_subdir_cannot_escape(tmp_path: Path):
    root = _fake_mathlib(tmp_path)
    assert "escapes" in grep_mathlib(root, "secret", subdir="../../")


def test_ripgrep_search_has_global_limit_and_safe_literal_kind(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(
            stdout=("A.lean:1:theorem first : True := trivial\n"
                    "B.lean:2:theorem second : True := trivial\n"
                    "C.lean:3:theorem third : True := trivial\n"),
            returncode=0,
        )

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    result = core._grep_with_rg(
        "/usr/bin/rg", tmp_path, "-foo", "theorem", 2, 0, True
    )
    command = captured["command"]
    expression = command[command.index("--regexp") + 1]
    assert expression.startswith(r"^theorem\s+.*")
    assert r"\-foo" in expression
    assert "-F" not in command
    assert "Found 2 matches" in result
    assert "third" not in result

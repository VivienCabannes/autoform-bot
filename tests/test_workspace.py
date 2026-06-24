"""Tests for the workspace inspector's targets parsing.

Guards the fallback YAML parser (used when PyYAML is absent, e.g. `make demo`
with plain python3): inline lists and booleans must parse to real Python
values, not strings like "[a]" / "false".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_inspect(repo_root: Path):
    path = repo_root / "skills" / "workspace" / "inspect.py"
    spec = importlib.util.spec_from_file_location("autoform_inspect", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_coerce_scalar(repo_root: Path):
    """The fallback coercion turns flow lists / bools into real values."""
    insp = _load_inspect(repo_root)
    assert insp._coerce_scalar("[]") == []
    assert insp._coerce_scalar("[a, b]") == ["a", "b"]
    assert insp._coerce_scalar("[def-convex-set]") == ["def-convex-set"]
    assert insp._coerce_scalar("false") is False
    assert insp._coerce_scalar("true") is True
    assert insp._coerce_scalar("null") is None
    assert insp._coerce_scalar("hello") == "hello"
    assert insp._coerce_scalar("'quoted'") == "quoted"


def test_targets_dependencies_parse_as_list(repo_root: Path, tmp_path: Path, monkeypatch):
    """End-to-end: a targets file yields list-typed `dependencies` + bool flags.

    Forces the no-PyYAML fallback (the path `make demo` uses) so this genuinely
    guards the fallback parser even in environments where PyYAML is installed.
    """
    import sys

    monkeypatch.setitem(sys.modules, "yaml", None)  # makes `import yaml` raise → fallback
    insp = _load_inspect(repo_root)
    (tmp_path / "targets.yaml").write_text(
        "- id: a\n"
        "  dependencies: []\n"
        "  proved_in_book: false\n"
        "- id: b\n"
        "  dependencies: [a]\n"
        "  proved_in_book: true\n",
        encoding="utf-8",
    )
    result = insp.list_targets(str(tmp_path))
    by_id = {t["id"]: t for t in result["targets"]}
    assert by_id["a"]["dependencies"] == []          # not the string "[]"
    assert by_id["b"]["dependencies"] == ["a"]        # not the string "[a]"
    assert by_id["a"]["proved_in_book"] is False      # not the string "false"
    assert by_id["b"]["proved_in_book"] is True

#!/usr/bin/env python3
"""Static lint for the autoform Claude Code plugin.

The plugin is markdown + JSON, so there is no logic to unit-test — but the
artifacts can still rot: invalid JSON, a command missing frontmatter, a skill
pointing at a reference file that was renamed, or a leftover reference to a
command/agent that was deleted in the 0.2.0 redesign. This script catches all of
that with the standard library only (no PyYAML, no autoform package), so it runs
in CI without installing anything.

Exit code 0 = clean, 1 = at least one error. Run: python plugins/autoform/scripts/lint_plugin.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve()
PLUGIN_ROOT = SCRIPT.parents[1]          # plugins/autoform
REPO_ROOT = SCRIPT.parents[3]            # repo root (holds .claude-plugin/marketplace.json)

# Commands and agents removed in the 0.2.0 redesign — a surviving reference is a regression.
REMOVED_COMMANDS = ("extract", "formalize", "orchestrate", "eval")
REMOVED_AGENTS = ("extractor", "extraction-reviewer", "merger", "orchestrator")

errors: list[str] = []
checks = 0


def err(msg: str) -> None:
    errors.append(msg)


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def frontmatter(path: Path) -> dict[str, str] | None:
    """Return top-level frontmatter keys → first-line value, or None if absent."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    keys: dict[str, str] = {}
    for line in block.splitlines():
        m = re.match(r"([a-z][a-zA-Z0-9_-]*):(.*)", line)
        if m:
            keys[m.group(1)] = m.group(2).strip()
    return keys


def load_json(path: Path) -> dict | None:
    global checks
    checks += 1
    if not path.exists():
        err(f"missing JSON file: {rel(path)}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        err(f"invalid JSON in {rel(path)}: {e}")
        return None


def check_marketplace() -> None:
    global checks
    data = load_json(REPO_ROOT / ".claude-plugin" / "marketplace.json")
    if data is None:
        return
    for key in ("name", "plugins"):
        checks += 1
        if key not in data:
            err(f"marketplace.json missing required key: {key}")
    for plugin in data.get("plugins", []):
        for key in ("name", "source", "description"):
            checks += 1
            if key not in plugin:
                err(f"marketplace.json plugin entry missing key: {key}")
        src = plugin.get("source")
        if src:
            checks += 1
            target = (REPO_ROOT / src / ".claude-plugin" / "plugin.json").resolve()
            if not target.exists():
                err(f"marketplace.json source '{src}' has no .claude-plugin/plugin.json")


def check_plugin_json() -> None:
    global checks
    data = load_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    if data is None:
        return
    for key in ("name", "version", "description"):
        checks += 1
        if key not in data:
            err(f"plugin.json missing required key: {key}")
    version = data.get("version", "")
    checks += 1
    if not re.match(r"^\d+\.\d+\.\d+", str(version)):
        err(f"plugin.json version is not semver-shaped: {version!r}")


def check_markdown_frontmatter() -> None:
    global checks
    # Commands: need a `description`.
    for path in sorted((PLUGIN_ROOT / "commands").glob("*.md")):
        checks += 1
        fm = frontmatter(path)
        if fm is None:
            err(f"{rel(path)}: no frontmatter block")
            continue
        if "description" not in fm:
            err(f"{rel(path)}: command frontmatter missing `description`")
    # Agents: need `name` (== filename stem) and `description`.
    for path in sorted((PLUGIN_ROOT / "agents").glob("*.md")):
        checks += 1
        fm = frontmatter(path)
        if fm is None:
            err(f"{rel(path)}: no frontmatter block")
            continue
        for key in ("name", "description"):
            if key not in fm:
                err(f"{rel(path)}: agent frontmatter missing `{key}`")
        if fm.get("name") and fm["name"] != path.stem:
            err(f"{rel(path)}: agent `name: {fm['name']}` != filename `{path.stem}`")
    # Skills: each SKILL.md needs `name` and `description`.
    for skill_md in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")):
        checks += 1
        fm = frontmatter(skill_md)
        if fm is None:
            err(f"{rel(skill_md)}: no frontmatter block")
            continue
        for key in ("name", "description"):
            if key not in fm:
                err(f"{rel(skill_md)}: skill frontmatter missing `{key}`")


def check_skill_references() -> None:
    """Every `*.md` named in a SKILL.md must exist in that skill's references/."""
    global checks
    for skill_md in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")):
        refs_dir = skill_md.parent / "references"
        body = skill_md.read_text(encoding="utf-8")
        for name in re.findall(r"`([a-zA-Z0-9_./-]+\.md)`", body):
            base = Path(name).name
            checks += 1
            if not (refs_dir / base).exists():
                err(f"{rel(skill_md)}: references `{base}` but {rel(refs_dir)}/{base} is missing")


def check_no_dangling_references() -> None:
    """No surviving references to commands/agents removed in 0.2.0."""
    global checks
    md_files = list(PLUGIN_ROOT.rglob("*.md"))
    removed_cmd = re.compile(r"/autoform:(" + "|".join(REMOVED_COMMANDS) + r")(?![\w-])")
    removed_agent = re.compile(r"\*\*(" + "|".join(REMOVED_AGENTS) + r")\*\*")
    for path in md_files:
        text = path.read_text(encoding="utf-8")
        checks += 1
        for m in removed_cmd.finditer(text):
            err(f"{rel(path)}: references removed command `/autoform:{m.group(1)}`")
        for m in removed_agent.finditer(text):
            err(f"{rel(path)}: references removed agent `**{m.group(1)}**`")


def main() -> int:
    if not (PLUGIN_ROOT / "commands").is_dir():
        print(f"error: plugin root not found at {PLUGIN_ROOT}", file=sys.stderr)
        return 1
    check_marketplace()
    check_plugin_json()
    check_markdown_frontmatter()
    check_skill_references()
    check_no_dangling_references()

    if errors:
        print(f"plugin-lint: FAILED ({len(errors)} error(s), {checks} checks)")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"plugin-lint: OK ({checks} checks passed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

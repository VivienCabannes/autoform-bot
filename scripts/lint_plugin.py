#!/usr/bin/env python3
"""Static lint for the host-neutral Autoform plugin (root layout).

The plugin surface is Markdown + JSON + TOML and can rot independently of the
Python suite: invalid manifests, non-portable skill frontmatter, broken
hooks/MCP paths, or stale role references. This script catches those failures
with the standard library only, so it runs without installing the package.

This is a root-level plugin: host manifests, `agents/`, `skills/`, hooks, and MCP
configuration all sit at the repository root.

Checks (all stdlib):
  - `.claude-plugin/marketplace.json` is valid JSON with `name`/`plugins`, and
    every plugin entry has `name`/`source`/`description` and a resolvable
    `<source>/.claude-plugin/plugin.json`.
  - `.claude-plugin/plugin.json` is valid JSON with `name`/`version`/`description`
    and a semver-shaped `version`.
  - Every `agents/*.md` has frontmatter with `name` (== filename) + `description`.
  - Every `skills/*/SKILL.md` has frontmatter with `name` + `description`.
  - Every `commands/*` (`.md` or `.toml`) carries a `description`.
  - Every `references/<file>` a SKILL.md cites exists in that skill's `references/`.
  - No surviving mention of an agent/skill in REMOVED_AGENTS / REMOVED_SKILLS
    (the rename-regression guard; HTML/`<!-- -->` comments are stripped first so
    planned-but-unbuilt TODO notes never trip it).
  - At least one agent and one skill were actually checked (so wholesale
    deletion/renaming of `agents/` or `skills/` cannot produce a vacuous pass).

Exit code 0 = clean, 1 = at least one error. Run: python3 scripts/lint_plugin.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve()
REPO_ROOT = SCRIPT.parents[1]            # root-level plugin: scripts/.. == repo root

# Agents / skills removed or renamed by a later PR. A surviving mention (outside
# an HTML comment) is a regression — add the OLD name here when you rename so any
# straggler reference is caught. Empty on the pristine niket/dev tree.
REMOVED_AGENTS: tuple[str, ...] = ()
REMOVED_SKILLS: tuple[str, ...] = ()
EXPECTED_MCP_SERVERS = frozenset(
    {
        "lean-informal-planner-mathlib",
        "autoform-repl",
        "autoform-lsp",
        "autoform-aristotle",
        "autoform-prover",
        "autoform-zulip",
    }
)

errors: list[str] = []
checks = 0

# Strip ``<!-- ... -->`` (incl. multi-line) so planned-but-unbuilt TODO notes,
# which legitimately mention not-yet-created skills, never trip the guards.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def err(msg: str) -> None:
    errors.append(msg)


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


def frontmatter(path: Path) -> dict[str, str] | None:
    """Return top-level frontmatter keys → first-line value, or None if absent.

    Tolerates leading blank lines / a leading BOM before the opening ``---``.
    """
    text = path.read_text(encoding="utf-8").lstrip("﻿")
    if not text.lstrip().startswith("---"):
        return None
    text = text.lstrip()
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end]
    keys: dict[str, str] = {}
    for line in block.splitlines():
        m = re.match(r"([a-zA-Z][a-zA-Z0-9_-]*):(.*)", line)
        if m:
            keys[m.group(1)] = m.group(2).strip()
    return keys


def toml_top_keys(path: Path) -> set[str]:
    """Top-level bare keys of a flat command .toml (no `tomllib` needed)."""
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("["):
            continue
        m = re.match(r'([A-Za-z0-9_-]+)\s*=', s)
        if m:
            keys.add(m.group(1))
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
            # `source: "./"` resolves to the repo root itself (root-level plugin).
            target = (REPO_ROOT / src / ".claude-plugin" / "plugin.json").resolve()
            if not target.exists():
                err(f"marketplace.json source {src!r} has no .claude-plugin/plugin.json")


def check_plugin_json() -> None:
    global checks
    data = load_json(REPO_ROOT / ".claude-plugin" / "plugin.json")
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
    servers = data.get("mcpServers")
    checks += 1
    if not isinstance(servers, dict):
        err(".claude-plugin/plugin.json: mcpServers must be an object")
    elif set(servers) != EXPECTED_MCP_SERVERS:
        err(
            ".claude-plugin/plugin.json: MCP server set differs from the "
            f"portable contract (got {', '.join(sorted(servers))})"
        )
    root_mcp = load_json(REPO_ROOT / ".mcp.json")
    if root_mcp is not None:
        root_servers = root_mcp.get("mcpServers")
        checks += 1
        if not isinstance(root_servers, dict) or set(root_servers) != EXPECTED_MCP_SERVERS:
            err(".mcp.json: MCP server set differs from the portable contract")


def check_codex_plugin() -> None:
    """Validate Codex's manifest and portable MCP configuration."""
    global checks
    manifest_path = REPO_ROOT / ".codex-plugin" / "plugin.json"
    data = load_json(manifest_path)
    if data is None:
        return
    for key in ("name", "version", "description", "skills", "mcpServers"):
        checks += 1
        if key not in data:
            err(f".codex-plugin/plugin.json missing required key: {key}")
    claude = load_json(REPO_ROOT / ".claude-plugin" / "plugin.json")
    if claude is not None:
        checks += 1
        if data.get("version") != claude.get("version"):
            err(
                "Claude and Codex plugin versions differ: "
                f"{claude.get('version')!r} != {data.get('version')!r}"
            )
    skills = data.get("skills")
    if isinstance(skills, str):
        checks += 1
        if not (REPO_ROOT / skills).is_dir():
            err(f"Codex skills path does not resolve: {skills!r}")
    # Current Codex discovers this default plugin-bundled location without a
    # manifest override; it also satisfies older ingestion validators that do
    # not yet accept the documented `hooks` manifest field.
    hook_path = REPO_ROOT / "hooks" / "hooks.json"
    hook_data = load_json(hook_path)
    if hook_data is not None:
        checks += 1
        if not isinstance(hook_data.get("hooks"), dict):
            err(f"{rel(hook_path)}: top-level hooks object is missing")
        rendered_hooks = json.dumps(hook_data)
        checks += 1
        if "${PLUGIN_ROOT}/hooks/session-start" not in rendered_hooks:
            err(
                f"{rel(hook_path)}: SessionStart does not resolve through "
                "the plugin-root environment"
            )
        checks += 1
        if not (REPO_ROOT / "hooks" / "session-start").is_file():
            err(f"{rel(hook_path)}: hooks/session-start does not exist")
    mcp_rel = data.get("mcpServers")
    if not isinstance(mcp_rel, str):
        return
    mcp_path = REPO_ROOT / mcp_rel
    mcp = load_json(mcp_path)
    if mcp is None:
        return
    servers = mcp.get("mcpServers")
    checks += 1
    if not isinstance(servers, dict) or not servers:
        err(f"{rel(mcp_path)}: mcpServers must be a non-empty object")
        return
    checks += 1
    if set(servers) != EXPECTED_MCP_SERVERS:
        err(f"{rel(mcp_path)}: MCP server set differs from the portable contract")
    for name, config in servers.items():
        checks += 1
        if not isinstance(config, dict) or not config.get("command"):
            err(f"{rel(mcp_path)}: server {name!r} has no command")
            continue


def check_agents() -> int:
    """Every agents/*.md needs `name` (== filename stem) and `description`.

    Returns the number of agents checked (0 would mean a vacuous pass)."""
    global checks
    count = 0
    for path in sorted((REPO_ROOT / "agents").glob("*.md")):
        count += 1
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
    return count


def check_skills() -> int:
    """Every skills/*/SKILL.md needs `name` and `description`.

    Returns the number of skills checked (0 would mean a vacuous pass)."""
    global checks
    count = 0
    for skill_md in sorted((REPO_ROOT / "skills").glob("*/SKILL.md")):
        count += 1
        checks += 1
        fm = frontmatter(skill_md)
        if fm is None:
            err(f"{rel(skill_md)}: no frontmatter block")
            continue
        for key in ("name", "description"):
            if key not in fm:
                err(f"{rel(skill_md)}: skill frontmatter missing `{key}`")
        extras = set(fm) - {"name", "description"}
        if extras:
            err(
                f"{rel(skill_md)}: non-portable skill frontmatter key(s): "
                f"{', '.join(sorted(extras))}"
            )
    return count


def check_commands() -> None:
    """Every command (`.md` frontmatter or `.toml`) needs a `description`."""
    global checks
    cmd_dir = REPO_ROOT / "commands"
    if not cmd_dir.is_dir():
        return
    for path in sorted(cmd_dir.iterdir()):
        if path.suffix == ".md":
            checks += 1
            fm = frontmatter(path)
            if fm is None:
                err(f"{rel(path)}: no frontmatter block")
            elif "description" not in fm:
                err(f"{rel(path)}: command frontmatter missing `description`")
        elif path.suffix == ".toml":
            checks += 1
            if "description" not in toml_top_keys(path):
                err(f"{rel(path)}: command .toml missing `description`")


def check_skill_references() -> None:
    """Every `references/<file>` a SKILL.md cites must exist in references/.

    niket/dev cites a skill's own reference files by their `references/`-rooted
    path (e.g. `references/plan-json-schema.md`). We match exactly that shape so
    data-file mentions (`informal_content/<id>.md`, `graph.json`) are ignored.
    Nested paths (`references/sub/x.md`) and any extension (`.py`, `.sh`, ...)
    are covered; trailing punctuation that markdown prose attaches (a sentence
    period, `...`) is stripped before checking existence.
    Backtick-quoted and bare path forms are both accepted; HTML comments stripped.
    """
    global checks
    ref_cite = re.compile(r"references/([A-Za-z0-9_./-]+)")
    for skill_md in sorted((REPO_ROOT / "skills").glob("*/SKILL.md")):
        refs_dir = skill_md.parent / "references"
        body = strip_comments(skill_md.read_text(encoding="utf-8"))
        for base in sorted({m.rstrip("./") for m in ref_cite.findall(body)}):
            if not base:
                continue
            checks += 1
            if not (refs_dir / base).exists():
                err(f"{rel(skill_md)}: cites `references/{base}` but "
                    f"{rel(refs_dir)}/{base} is missing")


def _plugin_markdown() -> list[Path]:
    """Markdown on the plugin surface (agents/skills/commands) — not docs/examples."""
    out: list[Path] = []
    for sub in ("agents", "skills", "commands"):
        out.extend((REPO_ROOT / sub).rglob("*.md"))
    return sorted(out)


def check_no_dangling_references() -> None:
    """No surviving mention of a removed/renamed agent or skill (HTML comments
    stripped first, so planned-but-unbuilt TODO notes are exempt)."""
    global checks
    if not (REMOVED_AGENTS or REMOVED_SKILLS):
        return
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for name in REMOVED_AGENTS:
        patterns.append((f"agent `{name}`",
                         re.compile(r"\b" + re.escape(name) + r"\b")))
    for name in REMOVED_SKILLS:
        patterns.append((f"skill `{name}`",
                         re.compile(r"skills/" + re.escape(name) + r"\b")))
    for path in _plugin_markdown():
        text = strip_comments(path.read_text(encoding="utf-8"))
        checks += 1
        for label, pat in patterns:
            if pat.search(text):
                err(f"{rel(path)}: references removed/renamed {label}")


def main() -> int:
    global checks
    if not (REPO_ROOT / ".claude-plugin").is_dir():
        print(f"error: .claude-plugin/ not found at {REPO_ROOT} "
              f"(run from the plugin repo root)", file=sys.stderr)
        return 1
    check_marketplace()
    check_plugin_json()
    check_codex_plugin()
    n_agents = check_agents()
    n_skills = check_skills()
    check_commands()
    check_skill_references()
    check_no_dangling_references()

    # Minimum-count sanity: this plugin ships agents and skills, so checking
    # zero of either means agents/ or skills/ was deleted/renamed — a vacuous
    # pass, not a clean tree.
    checks += 2
    if n_agents == 0:
        err("no agents/*.md found — agents/ deleted or renamed?")
    if n_skills == 0:
        err("no skills/*/SKILL.md found — skills/ deleted or renamed?")

    if errors:
        print(f"plugin-lint: FAILED ({len(errors)} error(s), {checks} checks)")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"plugin-lint: OK ({checks} checks passed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

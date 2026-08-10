#!/usr/bin/env python3
"""Static lint for the host-neutral Autoform plugin (root layout).

The plugin surface is Markdown + JSON + TOML and can rot independently of the
Python suite: invalid manifests, non-portable skill frontmatter, broken MCP
paths, or stale role references. This script catches those failures
with the standard library only, so it runs without installing the package.

Claude and Codex use root-level manifests. Muse uses a native manifest template
that the package builder copies into a clean single-manifest distribution root.

Checks (all stdlib):
  - `.claude-plugin/marketplace.json` is valid JSON with `name`/`plugins`, and
    every plugin entry has `name`/`source`/`description` and a resolvable
    `<source>/.claude-plugin/plugin.json`.
  - `.claude-plugin/plugin.json` is valid JSON with `name`/`version`/`description`
    and a semver-shaped `version`.
  - The native Muse manifest exposes exactly the public native slash commands
    and the portable MCP server set without duplicate skill entries in Muse's
    completion menu.
  - Every `agents/*.md` has frontmatter with `name` (== filename) + `description`.
  - Every `skills/*/SKILL.md` has frontmatter with `name` + `description`.
  - The user-visible skill set is exactly `setup`, `roadmap`, `orchestrate`, and
    `evaluate`;
    supporting runbooks do not reappear as slash commands.
  - No legacy `commands/*` file adds an extra user-visible command.
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
REMOVED_AGENTS: tuple[str, ...] = ("autoform-reader",)
REMOVED_SKILLS: tuple[str, ...] = ("set-backend",)
EXPECTED_MCP_SERVERS = frozenset(
    {
        "lean-lsp-mcp",
        "autoform-prover",
    }
)
#: The user-facing surface. The Muse manifest's command set and the Codex
#: `defaultPrompt` enumerate the same skills; keep all three in step when the
#: surface changes.
PUBLIC_WORKFLOW_SKILLS = frozenset(
    {
        "setup",
        "roadmap",
        "orchestrate",
        "evaluate",
        "agent-review",
        "human-review",
        "develop-plugin",
    }
)
MUSE_MANIFEST = REPO_ROOT / "packaging" / "muse" / ".muse-plugin" / "plugin.json"
REQUIRED_INTERNAL_ASSETS = (
    "internal/runbooks/evaluation.md",
    "internal/runbooks/github-pages.md",
    "internal/runbooks/mathlib-style.md",
    "internal/runbooks/planning.md",
    "internal/runbooks/proving.md",
    "internal/runbooks/review.md",
    "internal/runbooks/visualization.md",
    "internal/runbooks/worker.md",
    "internal/runbooks/zulip.md",
    "internal/references/plan-json-schema.md",
    "internal/references/reviewer-packet.md",
    "internal/references/zulip-configuration.md",
    "internal/references/mathlib/lean4-syntax.md",
    "internal/references/mathlib/mathlib-conventions.md",
    "internal/references/mathlib/proof-patterns.md",
    "internal/references/mathlib/tactic-patterns.md",
    "internal/references/mathlib/type-coercions.md",
    "internal/references/proving/axiom-policy.md",
    "internal/references/proving/commit-and-submit.md",
    "internal/references/proving/false-statements.md",
    "internal/references/proving/proof-strategies.md",
    "internal/references/proving/sorry-handling.md",
    "internal/references/proving/tool-usage.md",
    "internal/rubrics/code_quality.json",
    "internal/rubrics/faithfulness.json",
    "internal/rubrics/proof_integrity.json",
    "scripts/install_autoform.sh",
    "scripts/install_lean.sh",
    "scripts/make_project.sh",
    "scripts/service_control.py",
    "scripts/workspace_inspector.py",
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


def _surface() -> str:
    """Name the public skills in an error, so the list cannot drift from the set."""
    return ", ".join(sorted(PUBLIC_WORKFLOW_SKILLS))


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
    mcp_reference = data.get("mcpServers")
    checks += 1
    if mcp_reference != "./.mcp.json":
        err(
            ".claude-plugin/plugin.json: mcpServers must reference "
            "the shared './.mcp.json' configuration"
        )
    checks += 1
    if "hooks" in data:
        err(".claude-plugin/plugin.json: Autoform must not inject session hooks")
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
    checks += 1
    if (REPO_ROOT / "hooks").exists():
        err("hooks/: Autoform must remain invocation-driven, not session-global")
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


def check_muse_plugin() -> None:
    """Validate the native Muse template and its source-tree capabilities."""
    global checks
    data = load_json(MUSE_MANIFEST)
    if data is None:
        return

    checks += 1
    if data.get("schemaVersion") != 1:
        err(f"{rel(MUSE_MANIFEST)}: schemaVersion must be 1")
    checks += 1
    if data.get("compat") != {"source": "native", "manifestDir": ".muse-plugin"}:
        err(f"{rel(MUSE_MANIFEST)}: invalid native compatibility declaration")

    claude = load_json(REPO_ROOT / ".claude-plugin" / "plugin.json")
    codex = load_json(REPO_ROOT / ".codex-plugin" / "plugin.json")
    if claude is not None and codex is not None:
        checks += 1
        if data.get("version") not in {claude.get("version"), codex.get("version")}:
            err(
                f"{rel(MUSE_MANIFEST)}: version {data.get('version')!r} does not "
                "match either shipping host manifest"
            )

    capabilities = data.get("capabilities")
    checks += 1
    if not isinstance(capabilities, dict):
        err(f"{rel(MUSE_MANIFEST)}: capabilities must be an object")
        return
    expected_kinds = {"skills", "commands", "mcpServers", "reminders"}
    checks += 1
    if set(capabilities) != expected_kinds:
        err(
            f"{rel(MUSE_MANIFEST)}: capability families must be exactly "
            f"{', '.join(sorted(expected_kinds))}"
        )

    skills = capabilities.get("skills")
    checks += 1
    if skills != []:
        err(f"{rel(MUSE_MANIFEST)}: Muse skills must be empty; commands own completion")

    commands = capabilities.get("commands")
    checks += 1
    if not isinstance(commands, list) or {
        item.get("id") for item in commands if isinstance(item, dict)
    } != PUBLIC_WORKFLOW_SKILLS:
        err(f"{rel(MUSE_MANIFEST)}: Muse command set differs from the public workflow")
    elif len(commands) != len(PUBLIC_WORKFLOW_SKILLS):
        err(f"{rel(MUSE_MANIFEST)}: Muse commands contain duplicate entries")
    else:
        for command in commands:
            expected_path = f"skills/{command['id']}/SKILL.md"
            checks += 1
            if command.get("path") != expected_path:
                err(
                    f"{rel(MUSE_MANIFEST)}: command {command['id']!r} must reuse "
                    f"the canonical skill at {expected_path!r}"
                )
            checks += 1
            if not (REPO_ROOT / expected_path).is_file():
                err(f"{rel(MUSE_MANIFEST)}: command path does not resolve: {expected_path!r}")
            checks += 1
            if command.get("enabledDefault") is not True:
                err(
                    f"{rel(MUSE_MANIFEST)}: command {command['id']!r} must be "
                    "enabled by default"
                )

    checks += 1
    if "hooks" in capabilities:
        err(f"{rel(MUSE_MANIFEST)}: Autoform must not inject session hooks")

    servers = capabilities.get("mcpServers")
    checks += 1
    if not isinstance(servers, list) or {
        item.get("id") for item in servers if isinstance(item, dict)
    } != EXPECTED_MCP_SERVERS:
        err(f"{rel(MUSE_MANIFEST)}: Muse MCP server set differs from the portable contract")
    elif len(servers) != len(EXPECTED_MCP_SERVERS):
        err(f"{rel(MUSE_MANIFEST)}: Muse MCP servers contain duplicate entries")
    else:
        for server in servers:
            command = server.get("command")
            checks += 1
            if (
                not isinstance(command, list)
                or command[:2] != ["bash", "servers/run-muse-server.sh"]
                or len(command) != 3
            ):
                err(
                    f"{rel(MUSE_MANIFEST)}: server {server.get('id')!r} does not "
                    "use the native Muse launcher"
                )

    for path in (REPO_ROOT / "servers" / "run-muse-server.sh", REPO_ROOT / "scripts" / "build_muse_plugin.py"):
        checks += 1
        if not path.is_file():
            err(f"missing Muse packaging asset: {rel(path)}")


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
        checks += 1
        if "model" in fm:
            err(
                f"{rel(path)}: agent frontmatter must not pin `model`; "
                "roles inherit the host-selected model"
            )
    return count


def check_skills() -> int:
    """Every skills/*/SKILL.md needs `name` and `description`.

    Returns the number of skills checked (0 would mean a vacuous pass)."""
    global checks
    count = 0
    found: set[str] = set()
    for skill_md in sorted((REPO_ROOT / "skills").glob("*/SKILL.md")):
        count += 1
        found.add(skill_md.parent.name)
        checks += 1
        fm = frontmatter(skill_md)
        if fm is None:
            err(f"{rel(skill_md)}: no frontmatter block")
            continue
        for key in ("name", "description"):
            if key not in fm:
                err(f"{rel(skill_md)}: skill frontmatter missing `{key}`")
        if fm.get("name") and fm["name"] != skill_md.parent.name:
            err(
                f"{rel(skill_md)}: skill `name: {fm['name']}` != directory "
                f"`{skill_md.parent.name}`"
            )
        extras = set(fm) - {"name", "description"}
        if extras:
            err(
                f"{rel(skill_md)}: non-portable skill frontmatter key(s): "
                f"{', '.join(sorted(extras))}"
            )
    for name in sorted(PUBLIC_WORKFLOW_SKILLS - found):
        checks += 1
        err(
            f"missing core workflow skill: skills/{name}/SKILL.md "
            f"({_surface()} must ship in every host)"
        )
    for name in sorted(found - PUBLIC_WORKFLOW_SKILLS):
        checks += 1
        err(
            f"unexpected user-facing skill: skills/{name}/SKILL.md "
            f"(Autoform exposes only {_surface()})"
        )
    expected_paths = {
        (REPO_ROOT / "skills" / name / "SKILL.md").resolve()
        for name in PUBLIC_WORKFLOW_SKILLS
    }
    for path in sorted(REPO_ROOT.rglob("SKILL.md")):
        relative_parts = path.relative_to(REPO_ROOT).parts
        if (
            any(part in {".git", ".venv", "dist"} for part in relative_parts)
            or path.resolve() in expected_paths
        ):
            continue
        checks += 1
        err(
            f"stray SKILL.md outside the public command surface: {rel(path)} "
            "(store supporting material under internal/)"
        )
    return count


def check_internal_assets() -> None:
    """The public workflows retain their supporting implementation material."""
    global checks
    for path in REQUIRED_INTERNAL_ASSETS:
        checks += 1
        if not (REPO_ROOT / path).is_file():
            err(f"missing internal workflow asset: {path}")


def check_commands() -> None:
    """Legacy command files would add a fourth user-visible slash command."""
    global checks
    cmd_dir = REPO_ROOT / "commands"
    if not cmd_dir.is_dir():
        return
    for path in sorted(cmd_dir.iterdir()):
        if path.suffix in {".md", ".toml"}:
            checks += 1
            err(
                f"legacy user-facing command is not allowed: {rel(path)} "
                "(use one of the public workflow skills)"
            )


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
    # A skill-local citation is `references/...` with nothing pathlike in front
    # of it. Any prefix means the target is rooted elsewhere and is not this
    # skill's to hold: `internal/references/...` is plugin-rooted and validated
    # through REQUIRED_INTERNAL_ASSETS, and a cross-skill link such as
    # `../setup/references/zulip.md` belongs to the skill it names.
    ref_cite = re.compile(r"(?<![\w./-])references/([A-Za-z0-9_./-]+)")
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
    """Markdown on the plugin surface (agents/skills/commands), not docs or fixtures."""
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
    check_muse_plugin()
    n_agents = check_agents()
    n_skills = check_skills()
    check_internal_assets()
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

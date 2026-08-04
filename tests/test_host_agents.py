from __future__ import annotations

from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from scripts.install_host_agents import (
    GENERATED_MARKER,
    discover_agents,
    install,
    render_codex_agent,
)


def test_codex_agents_cover_canonical_roles():
    agents = discover_agents()
    assert len(agents) >= 10
    assert {"autoform-worker", "splitter", "graph-reviewer"} <= {
        agent.name for agent in agents
    }


def test_rendered_agent_has_required_codex_contract():
    worker = next(agent for agent in discover_agents() if agent.name == "autoform-worker")
    rendered = render_codex_agent(worker)
    assert rendered.startswith(GENERATED_MARKER)
    assert 'name = "autoform_worker"' in rendered
    assert 'sandbox_mode = "workspace-write"' in rendered
    assert "developer_instructions =" in rendered
    assert "do not launch another agent host" in rendered

    read_only_role = next(
        agent for agent in discover_agents() if agent.name == "holistic-reviewer"
    )
    assert 'sandbox_mode = "read-only"' in render_codex_agent(read_only_role)


def test_install_is_idempotent_and_checkable(tmp_path: Path):
    changed, _ = install(tmp_path, "codex")
    assert changed == len(discover_agents())
    changed_again, _ = install(tmp_path, "codex")
    assert changed_again == 0
    check_changed, _ = install(tmp_path, "codex", check=True)
    assert check_changed == 0
    assert (tmp_path / ".codex" / "agents" / "autoform_worker.toml").exists()
    assert (tmp_path / ".codex" / "agents" / "autoform_splitter.toml").exists()
    for path in (tmp_path / ".codex" / "agents").glob("*.toml"):
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        assert {"name", "description", "developer_instructions"} <= set(parsed)
        assert parsed["sandbox_mode"] in {"read-only", "workspace-write"}


def test_install_refuses_user_managed_collision(tmp_path: Path):
    # Pick a late-sorting role to prove the preflight prevents partial writes.
    target = tmp_path / ".codex" / "agents" / "autoform_splitter.toml"
    target.parent.mkdir(parents=True)
    target.write_text('name = "mine"\n', encoding="utf-8")
    with pytest.raises(FileExistsError, match="user-managed"):
        install(tmp_path, "codex")
    assert list(target.parent.glob("autoform_worker.toml")) == []
    assert target.read_text(encoding="utf-8") == 'name = "mine"\n'


def test_install_removes_only_obsolete_generated_agents(tmp_path: Path):
    destination = tmp_path / ".codex" / "agents"
    destination.mkdir(parents=True)
    obsolete = destination / "old_autoform_role.toml"
    obsolete.write_text(f"{GENERATED_MARKER}\nname = \"old\"\n", encoding="utf-8")
    unrelated = destination / "my_role.toml"
    unrelated.write_text('name = "mine"\n', encoding="utf-8")

    changed, messages = install(tmp_path, "codex", check=True)
    assert changed == len(discover_agents()) + 1
    assert any(message.startswith("obsolete ") for message in messages)
    assert obsolete.exists()

    install(tmp_path, "codex")
    assert not obsolete.exists()
    assert unrelated.exists()


def test_install_refuses_agent_directory_symlink_escape(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".codex").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        install(tmp_path, "codex")
    assert list(outside.iterdir()) == []

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.claims import (
    CLAIM_REF_PREFIX,
    CLAIM_SCHEMA,
    LEGACY_BLOCK_SCHEMA,
    LEGACY_CLAIM_SCHEMA,
    ClaimBoard,
    MalformedLeaseError,
    author_claim_key,
    resource_claim_key,
)


def _bare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "claims.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True)
    return repo


def _plant_message(repo: Path, key: str, message: str) -> None:
    tree = subprocess.run(
        ["git", "mktree"], cwd=repo, input="", capture_output=True, text=True, check=True
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "commit-tree", tree, "-m", message],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    ).stdout.strip()
    subprocess.run(["git", "update-ref", CLAIM_REF_PREFIX + key, commit], cwd=repo, check=True)


def _article(path: Path, title: str, article_id: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = f"article_id: {article_id}\n" if article_id else ""
    path.write_text(f"---\n{metadata}---\n\n# {title}\n", encoding="utf-8")


def _blueprint(tmp_path: Path, *, article_id: str | None = "af_0123456789abcdef01234567") -> Path:
    blueprint = tmp_path / "blueprint"
    _article(blueprint / "roadmap/chapter/README.md", "Chapter", None)
    _article(blueprint / "roadmap/chapter/main-result.md", "Main result", article_id)
    return blueprint


def _args(repo: Path, scratch: Path, blueprint: Path, *command: str) -> list[str]:
    args = [
        "claim",
        *command,
        "--repo",
        str(repo),
        "--worker-id",
        "worker-a",
        "--session-id",
        "test-session",
        "--scratch",
        str(scratch),
    ]
    if command[0] in {"acquire", "renew", "release"}:
        args.extend(["--blueprint", str(blueprint)])
    return args


def test_claim_cli_acquire_renew_list_release_round_trip(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"

    assert main(_args(repo, scratch, blueprint, "acquire", node_id, "--ttl", "600")) == 0
    assert "acquired chapter/main-result" in capsys.readouterr().out
    assert main(_args(repo, scratch, blueprint, "renew", node_id, "--ttl", "600")) == 0
    assert "renewed chapter/main-result" in capsys.readouterr().out
    assert main(_args(repo, scratch, blueprint, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    by_key = {lease["_key"]: lease for lease in leases}
    durable_key = author_claim_key("af_0123456789abcdef01234567")
    legacy_key = author_claim_key(node_id)
    assert by_key[durable_key]["schema"] == CLAIM_SCHEMA
    assert by_key[durable_key]["owner"] == "worker-a"
    assert by_key[legacy_key]["schema"] == LEGACY_BLOCK_SCHEMA
    assert main(_args(repo, scratch, blueprint, "release", node_id)) == 0
    assert "released chapter/main-result" in capsys.readouterr().out


def test_claim_cli_refuses_live_peer_and_list_needs_no_session_identity(
    tmp_path: Path, capsys
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert main(_args(repo, first, blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()

    peer = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--repo",
        str(repo),
        "--worker-id",
        "worker-b",
        "--session-id",
        "peer-session",
        "--scratch",
        str(second),
        "--blueprint",
        str(blueprint),
    ]
    assert main(peer) == 1
    assert "ownership is held or unverifiable" in capsys.readouterr().out

    assert main(["claim", "list", "--repo", str(repo), "--scratch", str(second)]) == 0
    assert json.loads(capsys.readouterr().out)


def test_claim_cli_transport_failure_is_nonzero(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing" / "claims.git"
    blueprint = _blueprint(tmp_path)
    assert main(_args(missing, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "error:" in capsys.readouterr().out


def test_claim_cli_refuses_malformed_remote_lease(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"
    _plant_message(repo, author_claim_key(article_id), "not json")

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "invalid lease JSON" in capsys.readouterr().out


def test_nonexistent_article_creates_no_claim_ref(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "missing")) == 1
    assert "does not exist" in capsys.readouterr().out
    assert subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""


def test_article_without_durable_id_is_actionable_and_creates_no_ref(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path, article_id=None)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    output = capsys.readouterr().out
    assert "has no durable article_id" in output
    assert "autoform migrate article-ids" in output
    assert subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""


def test_article_rename_with_unchanged_id_preserves_claim_key(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, scratch, blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()
    old_path = blueprint / "roadmap/chapter/main-result.md"
    new_path = blueprint / "roadmap/chapter/renamed-result.md"
    old_path.rename(new_path)

    assert main(_args(repo, scratch, blueprint, "renew", "chapter/renamed-result")) == 0
    capsys.readouterr()
    assert main(_args(repo, scratch, blueprint, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    assert {lease["_key"] for lease in leases} == {
        author_claim_key("af_0123456789abcdef01234567"),
        author_claim_key("chapter/main-result"),
        author_claim_key("chapter/renamed-result"),
    }


def test_article_target_rejects_path_and_article_id_ambiguity(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    ambiguous = "af_aaaaaaaaaaaaaaaaaaaaaaaa"
    _article(blueprint / f"roadmap/{ambiguous}.md", "Path match", "af_bbbbbbbbbbbbbbbbbbbbbbbb")
    _article(blueprint / "roadmap/id-match.md", "ID match", ambiguous)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", ambiguous)) == 1
    assert "is ambiguous" in capsys.readouterr().out


def test_legacy_path_cannot_fence_another_articles_durable_key(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    second_id = "af_bbbbbbbbbbbbbbbbbbbbbbbb"
    _article(
        blueprint / "roadmap/af_0123456789abcdef01234567.md",
        "Colliding path",
        second_id,
    )

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", second_id)) == 1
    assert "collides with a durable canonical claim key" in capsys.readouterr().out
    assert subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""


def test_explicit_resource_uses_a_distinct_namespace_and_round_trips(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    scratch = tmp_path / "scratch"

    assert main(_args(repo, scratch, blueprint, "acquire", "--resource", "lake-build")) == 0
    capsys.readouterr()
    assert main(_args(repo, scratch, blueprint, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    by_key = {lease["_key"]: lease for lease in leases}
    assert by_key[resource_claim_key("lake-build")]["schema"] == CLAIM_SCHEMA
    assert by_key[author_claim_key("lake-build")]["schema"] == LEGACY_BLOCK_SCHEMA
    assert main(_args(repo, scratch, blueprint, "release", "--resource", "lake-build")) == 0


def test_resource_name_cannot_impersonate_a_durable_article_id(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(
        _args(
            repo,
            tmp_path / "scratch",
            blueprint,
            "acquire",
            "--resource",
            "af_0123456789abcdef01234567",
        )
    ) == 1
    assert "reserved article_id format" in capsys.readouterr().out


def test_positional_lake_build_is_resolved_as_an_article_not_a_resource(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_aaaaaaaaaaaaaaaaaaaaaaaa"
    _article(blueprint / "roadmap/lake-build.md", "Lake build article", article_id)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "lake-build")) == 0
    assert author_claim_key(article_id) in capsys.readouterr().out


def test_positional_lake_build_without_an_article_requires_explicit_resource(
    tmp_path: Path, capsys
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "lake-build")) == 1
    assert "use --resource lake-build" in capsys.readouterr().out


def test_article_and_resource_targets_are_mutually_exclusive(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(
        _args(
            repo,
            tmp_path / "scratch",
            blueprint,
            "acquire",
            "chapter/main-result",
            "--resource",
            "lake-build",
        )
    ) == 1
    assert "mutually exclusive" in capsys.readouterr().out


def test_live_legacy_path_claim_blocks_new_article_key(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "renewed_at": 100.0,
        "expires_at": 200.0,
        "resource": author_claim_key(node_id),
    }
    _plant_message(repo, author_claim_key(node_id), json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 150.0)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 1
    assert "live legacy v1 claim" in capsys.readouterr().out


def test_live_legacy_resource_key_blocks_new_resource_namespace(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    legacy_key = author_claim_key("lake-build")
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": legacy_key,
    }
    _plant_message(repo, legacy_key, json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 150.0)

    assert main(
        _args(repo, tmp_path / "scratch", blueprint, "acquire", "--resource", "lake-build")
    ) == 1
    assert "live legacy v1 claim" in capsys.readouterr().out
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert refs == [CLAIM_REF_PREFIX + legacy_key]


def test_renamed_live_legacy_path_blocks_durable_article_claim(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    old_id = "chapter/main-result"
    new_id = "chapter/renamed-result"
    (blueprint / "roadmap/chapter/main-result.md").rename(
        blueprint / "roadmap/chapter/renamed-result.md"
    )
    legacy_key = author_claim_key(old_id)
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": legacy_key,
    }
    _plant_message(repo, legacy_key, json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 150.0)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", new_id)) == 1
    assert "live legacy v1 claim" in capsys.readouterr().out

    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 201.0)
    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", new_id)) == 0
    capsys.readouterr()
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert board.read(legacy_key)["schema"] == LEGACY_BLOCK_SCHEMA


def test_d9_client_cannot_acquire_path_after_v2_owns_durable_id(
    tmp_path: Path, capsys
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    path_id = "chapter/main-result"
    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", path_id)) == 0
    capsys.readouterr()

    class D9Client(ClaimBoard):
        @staticmethod
        def _lease_is_valid(lease: dict[str, object], key: str | None = None) -> bool:
            return bool(
                lease.get("schema") == LEGACY_CLAIM_SCHEMA
                and ClaimBoard._lease_is_valid(lease, key)
            )

    old_client = D9Client(repo, "worker-a", tmp_path / "old-client")
    with pytest.raises(MalformedLeaseError, match="invalid lease schema"):
        old_client.acquire(author_claim_key(path_id), ttl=600)


def test_expired_legacy_path_claim_does_not_block_new_article_key(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": author_claim_key(node_id),
    }
    _plant_message(repo, author_claim_key(node_id), json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 201.0)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 0
    capsys.readouterr()
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert CLAIM_REF_PREFIX + author_claim_key(node_id) in refs
    assert CLAIM_REF_PREFIX + author_claim_key("af_0123456789abcdef01234567") in refs


def test_expired_v1_at_durable_key_is_upgraded_instead_of_permanently_blocked(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"
    canonical_key = author_claim_key(article_id)
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": canonical_key,
    }
    _plant_message(repo, canonical_key, json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 201.0)

    assert main(
        _args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")
    ) == 0
    capsys.readouterr()
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert board.read(canonical_key)["schema"] == CLAIM_SCHEMA


def test_malformed_legacy_path_claim_blocks_new_article_key(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"
    _plant_message(repo, author_claim_key(node_id), "not json")

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 1
    assert "invalid lease JSON" in capsys.readouterr().out
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert refs == [CLAIM_REF_PREFIX + author_claim_key(node_id)]


def test_cli_session_environment_is_stable_across_worker_label_changes(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("AUTOFORM_CLAIM_SESSION_ID", "worktree-session")
    monkeypatch.setenv("AUTOFORM_WORKER_ID", "worker-a")
    acquire = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--repo",
        str(repo),
        "--scratch",
        str(scratch),
        "--blueprint",
        str(blueprint),
    ]
    assert main(acquire) == 0
    capsys.readouterr()

    monkeypatch.setenv("AUTOFORM_WORKER_ID", "worker-b")
    renew = acquire.copy()
    renew[1] = "renew"
    assert main(renew) == 0
    assert "renewed" in capsys.readouterr().out


def test_cli_derives_a_stable_session_from_the_target_worktree(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    blueprint = _blueprint(project)
    scratch = tmp_path / "scratch"
    monkeypatch.delenv("AUTOFORM_CLAIM_SESSION_ID", raising=False)
    args = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--repo",
        str(repo),
        "--worker-id",
        "worker-a",
        "--scratch",
        str(scratch),
        "--blueprint",
        str(blueprint),
    ]

    assert main(args) == 0
    capsys.readouterr()
    args[1] = "renew"
    assert main(args) == 0
    assert "renewed" in capsys.readouterr().out


def test_blueprint_project_selects_that_projects_origin(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
    _blueprint(project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    args = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--worker-id",
        "worker-a",
        "--session-id",
        "session-a",
        "--scratch",
        str(tmp_path / "scratch"),
        "--blueprint",
        str(project),
    ]
    assert main(args) == 0
    assert "acquired" in capsys.readouterr().out


def test_cleanup_needs_no_worker_or_worktree_session(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = _bare_repo(tmp_path)
    key = "expired"
    lease = {
        "schema": CLAIM_SCHEMA,
        "lease_id": "1" * 64,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "renewed_at": 100.0,
        "expires_at": 200.0,
        "resource": key,
    }
    _plant_message(repo, key, json.dumps(lease))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    args = ["claim", "cleanup", "--repo", str(repo), "--scratch", str(tmp_path / "scratch")]

    assert main(args) == 0
    assert "recovered 1 expired or unsafe-timestamp claim(s)" in capsys.readouterr().out


def test_cleanup_with_blueprint_retires_old_paths_without_blocking_durable_ids(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    old_path_key = author_claim_key("chapter/old-result")
    canonical_key = author_claim_key("af_0123456789abcdef01234567")
    for key in (old_path_key, canonical_key):
        lease = {
            "schema": LEGACY_CLAIM_SCHEMA,
            "owner": "old-worker",
            "host": "old-host",
            "pid": 1,
            "acquired_at": 100.0,
            "expires_at": 200.0,
            "resource": key,
        }
        _plant_message(repo, key, json.dumps(lease))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert main(
        [
            "claim",
            "cleanup",
            "--repo",
            str(repo),
            "--scratch",
            str(tmp_path / "scratch"),
            "--blueprint",
            str(blueprint),
        ]
    ) == 0
    assert "recovered 2" in capsys.readouterr().out
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert board.read(old_path_key)["schema"] == LEGACY_BLOCK_SCHEMA
    assert board.read(canonical_key) is None

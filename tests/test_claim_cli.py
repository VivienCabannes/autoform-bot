from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from autoform_cli import __main__ as cli
from autoform_cli.__main__ import main
from autoform_cli.claims import (
    CLAIM_REF_PREFIX,
    CLAIM_SCHEMA,
    LEGACY_BLOCK_SCHEMA,
    LEGACY_CLAIM_SCHEMA,
    LEGACY_TOMBSTONE_SCHEMA,
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


def test_claim_cli_acquire_renew_list_release_round_trip(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 1_000.0)
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


def test_claim_cli_refuses_live_peer_and_list_needs_no_session_identity(tmp_path: Path, capsys) -> None:
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
    assert "ownership is held or unverifiable" in capsys.readouterr().err
    assert main(["claim", "list", "--repo", str(repo), "--scratch", str(second)]) == 0
    assert json.loads(capsys.readouterr().out)


def test_failed_renew_does_not_install_a_legacy_compatibility_ref(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert (
        main(
            _args(
                repo,
                tmp_path / "scratch",
                blueprint,
                "renew",
                "chapter/main-result",
            )
        )
        == 1
    )
    assert "ownership is held or unverifiable" in capsys.readouterr().err
    assert (
        subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


@pytest.mark.parametrize(
    "extra, message",
    [
        (("--ttl", "0"), "finite positive"),
        (("--note", "x" * 4097), "must not exceed"),
    ],
)
def test_invalid_acquire_inputs_create_no_compatibility_ref(
    tmp_path: Path,
    capsys,
    extra: tuple[str, str],
    message: str,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert (
        main(
            _args(
                repo,
                tmp_path / "scratch",
                blueprint,
                "acquire",
                "chapter/main-result",
                *extra,
            )
        )
        == 1
    )
    assert message in capsys.readouterr().err
    assert (
        subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


def test_invalid_renew_ttl_does_not_publish_a_renamed_path_fence(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)
    assert main(_args(repo, scratch, blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()
    (blueprint / "roadmap/chapter/main-result.md").rename(blueprint / "roadmap/chapter/renamed-result.md")

    assert (
        main(
            _args(
                repo,
                scratch,
                blueprint,
                "renew",
                "chapter/renamed-result",
                "--ttl",
                "0",
            )
        )
        == 1
    )
    assert "finite positive" in capsys.readouterr().err
    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert inspector.read(author_claim_key("chapter/renamed-result")) is None


def test_claim_cli_transport_failure_is_nonzero(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing" / "claims.git"
    blueprint = _blueprint(tmp_path)
    assert main(_args(missing, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "error:" in capsys.readouterr().err


def test_claim_list_outside_git_requests_repo_not_session(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert main(["claim", "list", "--scratch", str(tmp_path / "scratch")]) == 1
    error = capsys.readouterr().err
    assert "--repo is required" in error
    assert "--session-id" not in error


def test_claim_cli_passes_explicit_object_format(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "claims-sha256.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "--object-format=sha256", str(repo)],
        check=True,
    )
    blueprint = _blueprint(tmp_path)
    args = _args(repo, tmp_path / "scratch", blueprint, "list")
    args.extend(["--object-format", "sha1"])

    assert main(args) == 1
    assert "does not match expected" in capsys.readouterr().err


def test_claim_cli_refuses_malformed_remote_lease(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"
    _plant_message(repo, author_claim_key(article_id), "not json")

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "invalid lease JSON" in capsys.readouterr().err


def test_nonexistent_article_creates_no_claim_ref(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "missing")) == 1
    assert "does not exist" in capsys.readouterr().err
    assert (
        subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


def test_article_without_durable_id_is_actionable_and_creates_no_ref(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path, article_id=None)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    output = capsys.readouterr().err
    assert "has no durable article_id" in output
    assert "autoform migrate article-ids" in output
    assert (
        subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


def test_long_legacy_article_path_is_migrated_to_its_exact_existing_key(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    component = "long-segment-" + "x" * 180
    node_id = "/".join((component, component, component))
    article_id = "af_bbbbbbbbbbbbbbbbbbbbbbbb"
    _article(blueprint / "roadmap" / component / "README.md", "Long chapter", None)
    _article(
        blueprint / "roadmap" / component / component / "README.md",
        "Long section",
        None,
    )
    _article(blueprint / "roadmap" / f"{node_id}.md", "Long path", article_id)
    assert len(node_id.encode("utf-8")) > 512

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 0
    capsys.readouterr()
    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    compatibility = inspector.read(author_claim_key(node_id))
    assert compatibility is not None
    assert compatibility["schema"] == LEGACY_BLOCK_SCHEMA
    assert compatibility["canonical_resource"] == author_claim_key(article_id)


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


def test_renew_by_durable_id_after_rename_fences_the_current_path(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"

    assert main(_args(repo, scratch, blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()
    (blueprint / "roadmap/chapter/main-result.md").rename(blueprint / "roadmap/chapter/renamed-result.md")

    assert main(_args(repo, scratch, blueprint, "renew", article_id)) == 0
    capsys.readouterr()
    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    compatibility = inspector.read(author_claim_key("chapter/renamed-result"))
    assert compatibility is not None
    assert compatibility["schema"] == LEGACY_BLOCK_SCHEMA
    assert compatibility["canonical_resource"] == author_claim_key(article_id)


def test_release_by_durable_id_survives_missing_article_source(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"

    assert main(_args(repo, scratch, blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()
    (blueprint / "roadmap/chapter/main-result.md").unlink()

    assert main(_args(repo, scratch, blueprint, "release", article_id)) == 0
    assert "released" in capsys.readouterr().out
    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert inspector.read(author_claim_key(article_id)) is None


def test_release_by_durable_id_does_not_bypass_an_invalid_graph(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"
    key = author_claim_key(article_id)
    board = ClaimBoard(repo, "worker-a", scratch, session_id="test-session")
    assert board.acquire(key)
    original_oid = board.held_claim_oid(key)
    (blueprint / "roadmap/chapter/main-result.md").write_text(
        "---\narticle_id: [\n---\n",
        encoding="utf-8",
    )

    assert main(_args(repo, scratch, blueprint, "release", article_id)) == 1
    assert capsys.readouterr().err.startswith("error: ")
    assert board.held_claim_oid(key) == original_oid


def test_id_shaped_article_path_release_resolves_the_valid_graph_first(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    path_id = "af_aaaaaaaaaaaaaaaaaaaaaaaa"
    durable_id = "af_bbbbbbbbbbbbbbbbbbbbbbbb"
    _article(blueprint / f"roadmap/{path_id}.md", "ID-shaped path", durable_id)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", path_id)) == 0
    capsys.readouterr()
    assert main(_args(repo, tmp_path / "scratch", blueprint, "release", path_id)) == 0
    assert "released" in capsys.readouterr().out
    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert inspector.read(author_claim_key(durable_id)) is None


def test_article_target_rejects_path_and_article_id_ambiguity(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    ambiguous = "af_aaaaaaaaaaaaaaaaaaaaaaaa"
    _article(blueprint / f"roadmap/{ambiguous}.md", "Path match", "af_bbbbbbbbbbbbbbbbbbbbbbbb")
    _article(blueprint / "roadmap/id-match.md", "ID match", ambiguous)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", ambiguous)) == 1
    assert "is ambiguous" in capsys.readouterr().err


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
    assert "collides with a durable canonical claim key" in capsys.readouterr().err
    assert (
        subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


def test_canonical_key_cannot_reuse_another_articles_legacy_path(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"
    _article(
        blueprint / f"roadmap/{article_id}.md",
        "Colliding path",
        "af_bbbbbbbbbbbbbbbbbbbbbbbb",
    )

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "collides with a durable canonical claim key" in capsys.readouterr().err
    assert (
        subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


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


def test_resource_release_survives_a_broken_roadmap(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    scratch = tmp_path / "scratch"

    assert main(_args(repo, scratch, blueprint, "acquire", "--resource", "lake-build")) == 0
    capsys.readouterr()
    (blueprint / "roadmap/chapter/main-result.md").write_text(
        "---\narticle_id: [\n---\n",
        encoding="utf-8",
    )

    assert main(_args(repo, scratch, blueprint, "release", "--resource", "lake-build")) == 0
    assert "released" in capsys.readouterr().out
    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert inspector.read(resource_claim_key("lake-build")) is None


def test_resource_renew_requires_the_existing_legacy_fence(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)
    resource_key = resource_claim_key("lake-build")
    board = ClaimBoard(
        repo,
        "worker-a",
        scratch,
        session_id="test-session",
    )
    assert board.acquire(resource_key)
    original_oid = board.held_claim_oid(resource_key)

    assert main(_args(repo, scratch, blueprint, "renew", "--resource", "lake-build")) == 1
    assert "ownership is held or unverifiable" in capsys.readouterr().err
    assert board.held_claim_oid(resource_key) == original_oid
    assert board.read(author_claim_key("lake-build")) is None


def test_resource_name_cannot_impersonate_a_durable_article_id(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert (
        main(
            _args(
                repo,
                tmp_path / "scratch",
                blueprint,
                "acquire",
                "--resource",
                "af_0123456789abcdef01234567",
            )
        )
        == 1
    )
    assert "reserved article_id format" in capsys.readouterr().err


def test_resource_acquire_outside_a_project_requires_blueprint(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _bare_repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert (
        main(
            [
                "claim",
                "acquire",
                "--resource",
                "lake-build",
                "--repo",
                str(repo),
                "--worker-id",
                "worker-a",
                "--session-id",
                "test-session",
                "--scratch",
                str(tmp_path / "scratch"),
            ]
        )
        == 1
    )
    assert "require --blueprint" in capsys.readouterr().err
    assert (
        subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


def test_positional_lake_build_is_resolved_as_an_article_not_a_resource(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_aaaaaaaaaaaaaaaaaaaaaaaa"
    _article(blueprint / "roadmap/lake-build.md", "Lake build article", article_id)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "lake-build")) == 0
    assert author_claim_key(article_id) in capsys.readouterr().out


def test_positional_lake_build_without_an_article_requires_explicit_resource(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "lake-build")) == 1
    assert "use --resource lake-build" in capsys.readouterr().err


def test_article_and_resource_targets_are_mutually_exclusive(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert (
        main(
            _args(
                repo,
                tmp_path / "scratch",
                blueprint,
                "acquire",
                "chapter/main-result",
                "--resource",
                "lake-build",
            )
        )
        == 1
    )
    assert "mutually exclusive" in capsys.readouterr().err


def test_resource_cannot_reuse_an_existing_article_path(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert (
        main(
            _args(
                repo,
                tmp_path / "scratch",
                blueprint,
                "acquire",
                "--resource",
                "chapter/main-result",
            )
        )
        == 1
    )
    assert "collides with an existing article path" in capsys.readouterr().err
    assert (
        subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


def test_live_legacy_path_claim_blocks_new_article_key(tmp_path: Path, capsys, monkeypatch) -> None:
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
    assert "live legacy v1 claim" in capsys.readouterr().err


def test_live_legacy_resource_key_blocks_new_resource_namespace(tmp_path: Path, capsys, monkeypatch) -> None:
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

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "--resource", "lake-build")) == 1
    assert "live legacy v1 claim" in capsys.readouterr().err
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert refs == [CLAIM_REF_PREFIX + legacy_key]


def test_renamed_legacy_path_requires_cleanup_before_durable_article_claim(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    old_id = "chapter/main-result"
    new_id = "chapter/renamed-result"
    (blueprint / "roadmap/chapter/main-result.md").rename(blueprint / "roadmap/chapter/renamed-result.md")
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
    assert "ownership is held or unverifiable" in capsys.readouterr().err

    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 500.0)
    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", new_id)) == 1
    assert "ownership is held or unverifiable" in capsys.readouterr().err

    assert (
        main(
            _args(
                repo,
                tmp_path / "scratch",
                blueprint,
                "cleanup",
                "--blueprint",
                str(blueprint),
            )
        )
        == 0
    )
    capsys.readouterr()
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert board.read(legacy_key)["schema"] == LEGACY_TOMBSTONE_SCHEMA

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", new_id)) == 0


def test_d9_client_cannot_acquire_path_after_v2_owns_durable_id(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    path_id = "chapter/main-result"
    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", path_id)) == 0
    capsys.readouterr()

    class D9Client(ClaimBoard):
        @staticmethod
        def _lease_is_valid(lease: dict[str, object], key: str | None = None) -> bool:
            return bool(lease.get("schema") == LEGACY_CLAIM_SCHEMA and ClaimBoard._lease_is_valid(lease, key))

    old_client = D9Client(repo, "worker-a", tmp_path / "old-client")
    with pytest.raises(MalformedLeaseError, match="invalid lease schema"):
        old_client.acquire(author_claim_key(path_id), ttl=600)


def test_expired_legacy_path_claim_does_not_block_new_article_key(tmp_path: Path, capsys, monkeypatch) -> None:
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
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 500.0)

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
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 500.0)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert board.read(canonical_key)["schema"] == CLAIM_SCHEMA


def test_malformed_legacy_path_claim_blocks_new_article_key(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"
    _plant_message(repo, author_claim_key(node_id), "not json")

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 1
    assert "invalid lease JSON" in capsys.readouterr().err
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert refs == [CLAIM_REF_PREFIX + author_claim_key(node_id)]


def test_cli_session_environment_is_stable_across_worker_label_changes(tmp_path: Path, capsys, monkeypatch) -> None:
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


def test_cli_derives_a_stable_session_from_the_target_worktree(tmp_path: Path, capsys, monkeypatch) -> None:
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


def test_linked_worktree_resolves_origin_and_session_for_claims(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path / "remote")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=project,
        check=True,
    )
    subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
    _blueprint(project)
    subprocess.run(["git", "add", "blueprint"], cwd=project, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "blueprint"], cwd=project, check=True)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "--detach", str(linked)],
        cwd=project,
        check=True,
    )

    assert cli._origin_url(linked) == str(repo.resolve())
    assert cli._worktree_claim_session_id(linked) == cli._worktree_claim_session_id(linked)
    assert (
        main(
            [
                "claim",
                "acquire",
                "chapter/main-result",
                "--worker-id",
                "worker-a",
                "--scratch",
                str(tmp_path / "scratch"),
                "--blueprint",
                str(linked),
            ]
        )
        == 0
    )
    assert "acquired" in capsys.readouterr().out


def test_linked_worktree_uses_its_pinned_worktree_origin_config(tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path / "remote")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=project,
        check=True,
    )
    (project / "tracked").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked"], cwd=project, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "initial"], cwd=project, check=True)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "--detach", str(linked)],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "config", "extensions.worktreeConfig", "true"],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--worktree", "remote.origin.url", str(repo)],
        cwd=linked,
        check=True,
    )

    native = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=linked,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert native == str(repo)
    assert cli._origin_url(linked) == str(repo.resolve())

    common_repo = _bare_repo(tmp_path / "common")
    subprocess.run(
        ["git", "config", "remote.origin.url", str(common_repo)],
        cwd=project,
        check=True,
    )
    native = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=linked,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert native == str(common_repo)
    assert cli._origin_url(linked) == str(common_repo.resolve())

    second_common_repo = _bare_repo(tmp_path / "second-common")
    subprocess.run(
        ["git", "config", "--add", "remote.origin.url", str(second_common_repo)],
        cwd=project,
        check=True,
    )
    native = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=linked,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert native == str(common_repo)
    assert cli._origin_url(linked) == str(common_repo.resolve())


def test_linked_worktree_rejects_common_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=project, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "claims.git")],
        cwd=project,
        check=True,
    )
    (project / "tracked").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked"], cwd=project, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "initial"], cwd=project, check=True)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "--detach", str(linked)],
        cwd=project,
        check=True,
    )
    common = project / ".git"
    parked = tmp_path / "parked-git"
    replaced = False
    original_read = cli._read_stable_named_file

    def replace_before_config(directory_fd, name, **kwargs):
        nonlocal replaced
        if name == "config" and not replaced:
            common.rename(parked)
            common.mkdir()
            replaced = True
        return original_read(directory_fd, name, **kwargs)

    monkeypatch.setattr(cli, "_read_stable_named_file", replace_before_config)
    try:
        with pytest.raises(ValueError, match="replaced|changed"):
            cli._origin_url(linked)
    finally:
        if replaced:
            common.rmdir()
            parked.rename(common)


def test_existing_worktree_claim_token_is_read_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    token_path = git_dir / "autoform-claim-session"
    token_path.write_text("a" * 64 + "\n")
    real_open = cli.os.open

    def reject_token_writes(path, flags, *args, **kwargs):
        is_token = Path(path) == token_path or (os.fspath(path) == token_path.name and kwargs.get("dir_fd") is not None)
        if is_token and flags & (os.O_WRONLY | os.O_RDWR):
            raise AssertionError("an existing worktree token must not be rewritten")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(cli.os, "open", reject_token_writes)

    assert cli._worktree_claim_token(git_dir) == "a" * 64


def test_worktree_claim_token_recovers_a_published_installer_link(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    temporary = git_dir / (".autoform-claim-session-" + "b" * 32)
    temporary.write_text("a" * 64 + "\n", encoding="ascii")
    token_path = git_dir / "autoform-claim-session"
    os.link(temporary, token_path)
    assert token_path.stat().st_nlink == 2

    assert cli._worktree_claim_token(git_dir) == "a" * 64
    assert token_path.stat().st_nlink == 1
    assert not temporary.exists()


def test_worktree_claim_token_rejects_non_regular_file(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    os.mkfifo(git_dir / "autoform-claim-session")

    with pytest.raises(ValueError, match="must be a regular file"):
        cli._worktree_claim_token(git_dir)


def test_blueprint_project_selects_that_projects_origin(tmp_path: Path, capsys, monkeypatch) -> None:
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


def test_nested_blueprint_resolves_relative_origin_from_worktree_root(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path / "remote")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "../remote/claims.git"],
        cwd=project,
        check=True,
    )
    blueprint = _blueprint(project)

    assert (
        main(
            [
                "claim",
                "acquire",
                "--resource",
                "lake-build",
                "--worker-id",
                "worker-a",
                "--scratch",
                str(tmp_path / "scratch"),
                "--blueprint",
                str(blueprint),
            ]
        )
        == 0
    )
    assert "acquired" in capsys.readouterr().out
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert refs == sorted(
        [
            CLAIM_REF_PREFIX + author_claim_key("lake-build"),
            CLAIM_REF_PREFIX + resource_claim_key("lake-build"),
        ]
    )


def test_origin_url_preserves_scp_like_remote_without_user(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    origin = "git.example.test:team/claims.git"
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=project, check=True)

    assert cli._origin_url(project) == origin


def test_origin_url_ignores_inherited_and_local_url_rewrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intended = _bare_repo(tmp_path / "intended")
    redirected = _bare_repo(tmp_path / "redirected")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(intended)],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "config", f"url.{redirected}.insteadOf", str(intended)],
        cwd=project,
        check=True,
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{redirected}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(intended))

    assert cli._origin_url(project) == str(intended)


def test_claim_target_rejects_blueprint_path_replacement_after_resolution(tmp_path: Path, capsys, monkeypatch) -> None:
    intended_repo = _bare_repo(tmp_path / "intended")
    redirected_repo = _bare_repo(tmp_path / "redirected")
    intended_project = tmp_path / "project"
    redirected_project = tmp_path / "redirected-project"
    for project, repo in (
        (intended_project, intended_repo),
        (redirected_project, redirected_repo),
    ):
        project.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
        _blueprint(project)

    original_resolve = cli._resolve_claim_target
    pinned_project = tmp_path / "pinned-project"

    def resolve_then_replace(args, **kwargs):
        target = original_resolve(args, **kwargs)
        intended_project.rename(pinned_project)
        intended_project.symlink_to(redirected_project, target_is_directory=True)
        return target

    monkeypatch.setattr(cli, "_resolve_claim_target", resolve_then_replace)
    args = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--worker-id",
        "worker-a",
        "--scratch",
        str(tmp_path / "scratch"),
        "--blueprint",
        str(intended_project),
    ]

    assert main(args) == 1
    assert "changed while resolving the claim" in capsys.readouterr().err
    intended_refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=intended_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    redirected_refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=redirected_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert intended_refs == []
    assert redirected_refs == []


def test_claim_target_rejects_article_content_change_after_resolution(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap/chapter/main-result.md"
    original_resolve = cli._resolve_claim_target

    def resolve_then_edit(args, **kwargs):
        target = original_resolve(args, **kwargs)
        article.write_text(article.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        return target

    monkeypatch.setattr(cli, "_resolve_claim_target", resolve_then_edit)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "changed while resolving the claim" in capsys.readouterr().err
    assert (
        subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


def test_blueprint_change_after_legacy_fence_never_publishes_canonical_claim(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap/chapter/main-result.md"
    original_prepare = ClaimBoard.prepare_v2_claim

    def prepare_then_edit(self, *args, **kwargs):
        prepared = original_prepare(self, *args, **kwargs)
        article.write_text(
            article.read_text(encoding="utf-8") + "\nchanged\n",
            encoding="utf-8",
        )
        return prepared

    monkeypatch.setattr(ClaimBoard, "prepare_v2_claim", prepare_then_edit)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "changed while resolving the claim" in capsys.readouterr().err
    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    legacy = inspector.read(author_claim_key("chapter/main-result"))
    assert legacy is not None
    assert legacy["schema"] == LEGACY_BLOCK_SCHEMA
    assert legacy["canonical_resource"] == author_claim_key("af_0123456789abcdef01234567")
    assert inspector.read(author_claim_key("af_0123456789abcdef01234567")) is None


def test_post_acquire_rollback_restores_an_unpromoted_prior_generation(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap/chapter/main-result.md"
    target_key = author_claim_key("af_0123456789abcdef01234567")

    def expired_lease(key: str, lease_id: str) -> str:
        return json.dumps(
            {
                "schema": CLAIM_SCHEMA,
                "lease_id": lease_id,
                "owner": "old-worker",
                "host": "old-host",
                "pid": 1,
                "acquired_at": 100.0,
                "renewed_at": 100.0,
                "expires_at": 200.0,
                "resource": key,
            }
        )

    for index in range(9):
        key = author_claim_key(f"a{index:02d}")
        _plant_message(repo, key, expired_lease(key, f"{index + 1:064x}"))
    _plant_message(repo, target_key, expired_lease(target_key, "f" * 64))
    old_oid = subprocess.run(
        ["git", "rev-parse", CLAIM_REF_PREFIX + target_key],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 1_000.0)
    original_acquire = ClaimBoard.acquire

    def acquire_then_edit(self, key, *args, **kwargs):
        acquired = original_acquire(self, key, *args, **kwargs)
        if acquired and key == target_key:
            article.write_text(
                article.read_text(encoding="utf-8") + "\nchanged\n",
                encoding="utf-8",
            )
        return acquired

    monkeypatch.setattr(ClaimBoard, "acquire", acquire_then_edit)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "changed while resolving the claim" in capsys.readouterr().err
    restored_oid = subprocess.run(
        ["git", "rev-parse", CLAIM_REF_PREFIX + target_key],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert restored_oid == old_oid


def test_post_renew_blueprint_change_restores_the_prior_lease_and_receipt(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap/chapter/main-result.md"
    key = author_claim_key("af_0123456789abcdef01234567")
    assert main(_args(repo, scratch, blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()
    old_oid = subprocess.run(
        ["git", "rev-parse", CLAIM_REF_PREFIX + key],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    renew = ClaimBoard.renew

    def renew_then_edit(self, claim_key, *args, **kwargs):
        renewed = renew(self, claim_key, *args, **kwargs)
        if renewed and claim_key == key:
            article.write_text(
                article.read_text(encoding="utf-8") + "\nchanged\n",
                encoding="utf-8",
            )
        return renewed

    monkeypatch.setattr(ClaimBoard, "renew", renew_then_edit)

    assert main(_args(repo, scratch, blueprint, "renew", "chapter/main-result")) == 1
    assert "changed while resolving the claim" in capsys.readouterr().err
    owner = ClaimBoard(repo, "worker-a", scratch, session_id="test-session")
    assert owner.held_claim_oid(key) == old_oid


def test_claim_target_rejects_path_aba_during_origin_resolution(tmp_path: Path, capsys, monkeypatch) -> None:
    intended_repo = _bare_repo(tmp_path / "intended")
    redirected_repo = _bare_repo(tmp_path / "redirected")
    intended_project = tmp_path / "project"
    redirected_project = tmp_path / "redirected-project"
    for project, repo in (
        (intended_project, intended_repo),
        (redirected_project, redirected_repo),
    ):
        project.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
        _blueprint(project)

    original_values = cli._claim_context_values
    parked_project = tmp_path / "parked-project"

    def values_during_aba(context, **kwargs):
        intended_project.rename(parked_project)
        intended_project.symlink_to(redirected_project, target_is_directory=True)
        try:
            return original_values(context, **kwargs)
        finally:
            intended_project.unlink()
            parked_project.rename(intended_project)

    monkeypatch.setattr(cli, "_claim_context_values", values_during_aba)

    assert (
        main(
            [
                "claim",
                "acquire",
                "chapter/main-result",
                "--worker-id",
                "worker-a",
                "--scratch",
                str(tmp_path / "scratch"),
                "--blueprint",
                str(intended_project),
            ]
        )
        == 1
    )
    assert "replaced while resolving the claim" in capsys.readouterr().err
    for repo in (intended_repo, redirected_repo):
        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert refs == []


def test_claim_target_rejects_ancestor_aba_during_origin_resolution(tmp_path: Path, capsys, monkeypatch) -> None:
    intended_repo = _bare_repo(tmp_path / "intended")
    redirected_repo = _bare_repo(tmp_path / "redirected")
    project_root = tmp_path / "projects"
    intended_scope = project_root / "scope"
    redirected_scope = project_root / "other"
    intended_project = intended_scope / "inner" / "project"
    redirected_project = redirected_scope / "inner" / "project"
    for project, repo in (
        (intended_project, intended_repo),
        (redirected_project, redirected_repo),
    ):
        project.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
        _blueprint(project)

    original_values = cli._claim_context_values
    parked = project_root / "parked"

    def values_during_ancestor_aba(context, **kwargs):
        intended_scope.rename(parked)
        intended_scope.symlink_to(redirected_scope, target_is_directory=True)
        try:
            return original_values(context, **kwargs)
        finally:
            intended_scope.unlink()
            parked.rename(intended_scope)

    monkeypatch.setattr(cli, "_claim_context_values", values_during_ancestor_aba)

    assert (
        main(
            [
                "claim",
                "acquire",
                "chapter/main-result",
                "--worker-id",
                "worker-a",
                "--scratch",
                str(tmp_path / "scratch"),
                "--blueprint",
                str(intended_project),
            ]
        )
        == 1
    )
    assert "replaced while resolving the claim" in capsys.readouterr().err
    for repo in (intended_repo, redirected_repo):
        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert refs == []


def test_replacement_worktree_cannot_inherit_default_claim_session(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    project = tmp_path / "project"

    def initialize_worktree(path: Path) -> None:
        path.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=path, check=True)
        _blueprint(path)

    initialize_worktree(project)
    args = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--worker-id",
        "worker-a",
        "--scratch",
        str(tmp_path / "scratch"),
        "--blueprint",
        str(project),
    ]
    assert main(args) == 0
    capsys.readouterr()
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    key = author_claim_key("af_0123456789abcdef01234567")
    board._ensure_scratch()
    original_oid = board._remote_oid(key)

    project.rename(tmp_path / "original-project")
    initialize_worktree(project)
    args[1] = "renew"

    assert main(args) == 1
    assert "ownership is held or unverifiable" in capsys.readouterr().err
    assert board._remote_oid(key) == original_oid


def test_cleanup_rejects_blueprint_replacement_before_selecting_origin(tmp_path: Path, capsys, monkeypatch) -> None:
    intended_repo = _bare_repo(tmp_path / "intended")
    redirected_repo = _bare_repo(tmp_path / "redirected")
    intended_project = tmp_path / "project"
    redirected_project = tmp_path / "redirected-project"
    for project, repo in (
        (intended_project, intended_repo),
        (redirected_project, redirected_repo),
    ):
        project.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
        _blueprint(project)

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
    _plant_message(intended_repo, key, json.dumps(lease))
    original_load = cli.load_graph
    pinned_project = tmp_path / "pinned-project"

    def load_then_replace(blueprint):
        graph = original_load(blueprint)
        intended_project.rename(pinned_project)
        intended_project.symlink_to(redirected_project, target_is_directory=True)
        return graph

    monkeypatch.setattr(cli, "load_graph", load_then_replace)

    assert main(["claim", "cleanup", "--blueprint", str(intended_project)]) == 1
    assert "changed" in capsys.readouterr().err
    board = ClaimBoard(intended_repo, "inspector", tmp_path / "inspect-cleanup")
    assert board.read(key) is not None
    assert ClaimBoard(redirected_repo, "inspector", tmp_path / "inspect-redirected").list() == []


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


def test_cleanup_rejects_graph_wide_legacy_collision_before_mutation(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"
    _article(
        blueprint / f"roadmap/{article_id}.md",
        "Colliding path",
        "af_bbbbbbbbbbbbbbbbbbbbbbbb",
    )
    key = author_claim_key("chapter/old-result")
    _plant_message(
        repo,
        key,
        json.dumps(
            {
                "schema": LEGACY_CLAIM_SCHEMA,
                "owner": "old-worker",
                "host": "old-host",
                "pid": 1,
                "acquired_at": 100.0,
                "expires_at": 200.0,
                "resource": key,
            }
        ),
    )
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 1_000.0)

    assert (
        main(
            _args(
                repo,
                tmp_path / "scratch",
                blueprint,
                "cleanup",
                "--blueprint",
                str(blueprint),
            )
        )
        == 1
    )
    assert "collides with a durable canonical claim key" in capsys.readouterr().err
    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert inspector.read(key)["schema"] == LEGACY_CLAIM_SCHEMA


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

    assert (
        main(
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
        )
        == 0
    )
    assert "recovered 2" in capsys.readouterr().out
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert board.read(old_path_key)["schema"] == LEGACY_TOMBSTONE_SCHEMA
    assert board.read(canonical_key) is None


def test_cleanup_keeps_published_legacy_fence_if_blueprint_changes_afterward(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article = blueprint / "roadmap/chapter/main-result.md"
    legacy_key = author_claim_key("chapter/main-result")
    canonical_key = author_claim_key("af_0123456789abcdef01234567")
    _plant_message(
        repo,
        legacy_key,
        json.dumps(
            {
                "schema": LEGACY_CLAIM_SCHEMA,
                "owner": "old-worker",
                "host": "old-host",
                "pid": 1,
                "acquired_at": 100.0,
                "expires_at": 200.0,
                "resource": legacy_key,
            }
        ),
    )
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 1_000.0)
    cleanup = ClaimBoard.cleanup

    def cleanup_then_edit(self, **kwargs):
        recovered = cleanup(self, **kwargs)
        article.write_text(
            article.read_text(encoding="utf-8") + "\nchanged\n",
            encoding="utf-8",
        )
        return recovered

    monkeypatch.setattr(ClaimBoard, "cleanup", cleanup_then_edit)

    assert (
        main(
            _args(
                repo,
                tmp_path / "scratch",
                blueprint,
                "cleanup",
                "--blueprint",
                str(blueprint),
            )
        )
        == 1
    )
    assert "monotonic recovery" in capsys.readouterr().err
    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    compatibility = inspector.read(legacy_key)
    assert compatibility is not None
    assert compatibility["schema"] == LEGACY_BLOCK_SCHEMA
    assert compatibility["canonical_resource"] == canonical_key


def test_cleanup_preserves_identity_legacy_mapping_as_the_canonical_key(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_aaaaaaaaaaaaaaaaaaaaaaaa"
    _article(blueprint / f"roadmap/{article_id}.md", "Identity path", article_id)
    key = author_claim_key(article_id)
    _plant_message(
        repo,
        key,
        json.dumps(
            {
                "schema": LEGACY_CLAIM_SCHEMA,
                "owner": "old-worker",
                "host": "old-host",
                "pid": 1,
                "acquired_at": 100.0,
                "expires_at": 200.0,
                "resource": key,
            }
        ),
    )
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 1_000.0)

    assert (
        main(
            _args(
                repo,
                tmp_path / "scratch",
                blueprint,
                "cleanup",
                "--blueprint",
                str(blueprint),
            )
        )
        == 0
    )
    capsys.readouterr()
    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert inspector.read(key) is None

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", article_id)) == 0


def test_resource_migration_does_not_fence_an_unrelated_article_key(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_key = author_claim_key("af_0123456789abcdef01234567")
    resource_legacy_key = author_claim_key("lake-build")
    for key in (article_key, resource_legacy_key):
        _plant_message(
            repo,
            key,
            json.dumps(
                {
                    "schema": LEGACY_CLAIM_SCHEMA,
                    "owner": "old-worker",
                    "host": "old-host",
                    "pid": 1,
                    "acquired_at": 100.0,
                    "expires_at": 200.0,
                    "resource": key,
                }
            ),
        )
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 1_000.0)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "--resource", "lake-build")) == 0
    capsys.readouterr()

    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert inspector.read(article_key)["schema"] == LEGACY_CLAIM_SCHEMA
    assert inspector.read(resource_legacy_key)["schema"] == LEGACY_BLOCK_SCHEMA
    assert inspector.read(resource_claim_key("lake-build"))["schema"] == CLAIM_SCHEMA


def test_claim_failures_are_written_to_stderr(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "missing")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not exist" in captured.err


def test_invalid_scratch_is_reported_on_stderr_without_a_traceback(
    tmp_path: Path,
    capsys,
) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "not-a-directory"
    scratch.write_text("file", encoding="utf-8")

    result = main(
        [
            "claim",
            "list",
            "--repo",
            str(repo),
            "--scratch",
            str(scratch),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.startswith("error: ")

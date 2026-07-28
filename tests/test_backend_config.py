from __future__ import annotations

import json

import pytest

from scripts import backend_config


def test_every_configured_backend_maps_explicitly():
    assert backend_config.prover_of("max") == "claude"
    assert backend_config.prover_of("aristotle") == "aristotle"
    assert backend_config.prover_of("codex") == "codex"
    assert backend_config.prover_of("openai") == "openai"
    assert backend_config.prover_of("avocado") == "avocado"


def test_user_facing_labels_distinguish_cli_and_direct_api_backends():
    assert backend_config.BACKENDS["max"]["label"] == (
        "Claude Code (Max subscription)"
    )
    assert backend_config.BACKENDS["openai"]["label"] == (
        "Custom API (OpenAI-compatible)"
    )


def test_list_and_set_show_user_facing_labels(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUTOFORM_CONFIG", str(tmp_path / "config.json"))

    assert backend_config.main(["list", "--fallback", "codex"]) == 0
    listed = capsys.readouterr().out
    assert "Claude Code (Max subscription)" in listed
    assert "Custom API (OpenAI-compatible)" in listed

    assert backend_config.main(["set", "max"]) == 0
    selected = capsys.readouterr().out
    assert "backend set to 'max' — Claude Code (Max subscription)" in selected


def test_unknown_backend_fails_closed():
    with pytest.raises(ValueError, match="unknown backend"):
        backend_config.prover_of("codxe")


def test_openai_compatible_backends_round_trip(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    monkeypatch.setenv("AUTOFORM_CONFIG", str(config))
    backend_config.set_backend("avocado")
    assert backend_config.get_backend() == "avocado"
    assert json.loads(config.read_text())["backend"] == "avocado"


def test_host_native_fallback_only_applies_without_persisted_choice(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.json"
    monkeypatch.setenv("AUTOFORM_CONFIG", str(config))
    assert backend_config.get_backend("codex") == "codex"
    config.write_text('{"backend": "max"}', encoding="utf-8")
    assert backend_config.get_backend("codex") == "max"


def test_unknown_fallback_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOFORM_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="unknown fallback"):
        backend_config.get_backend("codxe")


@pytest.mark.parametrize("payload", ['{"backend":"codxe"}', "{not-json"])
def test_invalid_persisted_config_fails_closed(tmp_path, monkeypatch, payload):
    config = tmp_path / "config.json"
    config.write_text(payload, encoding="utf-8")
    monkeypatch.setenv("AUTOFORM_CONFIG", str(config))
    with pytest.raises(ValueError, match="backend"):
        backend_config.get_backend("codex")

#!/usr/bin/env python3
"""Check an OpenAI-compatible Autoform provider without sending project data."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from servers.prover.api_tools import ProjectTools, run_tool_loop  # noqa: E402
from servers.prover.openai_adapter import (  # noqa: E402
    _PRESETS,
    _env,
    _urllib_transport,
)


def resolve(provider: str) -> dict[str, Any]:
    if provider not in _PRESETS:
        raise ValueError(f"unknown provider {provider!r}; expected openai or avocado")
    default_base, default_model, default_key = _PRESETS[provider]
    base_url = (_env(provider, "BASE_URL") or default_base).rstrip("/")
    model = _env(provider, "MODEL") or default_model
    key_var = _env(provider, "KEY_VAR") or default_key
    raw_headers = _env(provider, "EXTRA_HEADERS")
    headers = json.loads(raw_headers) if raw_headers else {}
    if not isinstance(headers, dict):
        raise ValueError("EXTRA_HEADERS must be a JSON object")
    return {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "key_var": key_var,
        "credential_present": bool(os.environ.get(key_var, "").strip()),
        "extra_header_names": sorted(str(key) for key in headers),
        "_headers": headers,
    }


def probe(provider: str, *, transport: Any | None = None, timeout: float = 30.0) -> dict:
    """Run a minimal tool-call probe against a temporary marker file."""
    config = resolve(provider)
    missing = [
        name
        for name in ("base_url", "model")
        if not str(config.get(name) or "").strip()
    ]
    if missing:
        raise RuntimeError(f"missing provider setting(s): {', '.join(missing)}")
    if not config["credential_present"]:
        raise RuntimeError(f"credential env var {config['key_var']!r} is empty")
    marker = "AUTOFORM_PROVIDER_TOOL_PROBE_OK"
    with tempfile.TemporaryDirectory(prefix="autoform-provider-probe-") as directory:
        root = Path(directory)
        (root / "marker.txt").write_text(marker, encoding="utf-8")
        secret = os.environ[config["key_var"]].strip()
        final, usage, transcript = run_tool_loop(
            transport or _urllib_transport,
            url=f"{config['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {secret}",
                **config["_headers"],
            },
            model=config["model"],
            system_prompt=(
                "This is a provider capability probe. Use read_file on marker.txt, "
                "then return exactly its content. Do not call any other path."
            ),
            user_prompt="Read marker.txt with the provided tool and return the marker.",
            tools=ProjectTools(root, writable=False),
            timeout=timeout,
            max_turns=4,
        )
    tool_calls = sum(int(item.get("tool_calls") or 0) for item in transcript)
    return {
        "ok": marker in final and tool_calls > 0,
        "tool_calls": tool_calls,
        "turns": usage["turns"],
        "provider": provider,
        "model": config["model"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=sorted(_PRESETS))
    parser.add_argument(
        "--live",
        action="store_true",
        help="send a minimal temporary-marker tool-call probe",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    try:
        config = resolve(args.provider)
        public = {key: value for key, value in config.items() if not key.startswith("_")}
        print(json.dumps(public, indent=2))
        missing = [
            key
            for key in ("base_url", "model")
            if not str(config.get(key) or "").strip()
        ]
        if missing or not config["credential_present"]:
            print(
                "provider configuration is incomplete; no network request made",
                file=sys.stderr,
            )
            return 1
        if args.live:
            result = probe(args.provider, timeout=args.timeout)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 2
        print("configuration present; pass --live for a no-project-data tool probe")
        return 0
    except Exception as error:
        print(f"provider check failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

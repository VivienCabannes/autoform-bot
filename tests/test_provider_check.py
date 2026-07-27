from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import provider_check


def test_cli_rejects_nonpositive_timeout():
    with pytest.raises(SystemExit) as error:
        provider_check.main(["openai", "--timeout", "0"])
    assert error.value.code == 2


def test_resolve_never_returns_secret(monkeypatch):
    monkeypatch.setenv("AUTOFORM_AVOCADO_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("AUTOFORM_AVOCADO_MODEL", "avocado-test")
    monkeypatch.setenv("AUTOFORM_AVOCADO_KEY_VAR", "PRIVATE_TOKEN")
    monkeypatch.setenv("PRIVATE_TOKEN", "do-not-print")
    config = provider_check.resolve("avocado")
    assert config["credential_present"] is True
    assert "do-not-print" not in repr(config)


def test_live_probe_requires_and_observes_tool_call(monkeypatch):
    monkeypatch.setenv("AUTOFORM_OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "read-marker",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"marker.txt"}',
                            },
                        }],
                    },
                }],
            }
        marker = payload["messages"][-1]["content"]
        return {"choices": [{"message": {"content": marker}}]}

    result = provider_check.probe("openai", transport=transport)
    assert result["ok"] is True
    assert result["tool_calls"] == 1
    assert result["turns"] == 2


def test_live_probe_over_real_loopback_http(monkeypatch):
    requests: list[dict] = []
    authorizations: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler contract
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            requests.append(payload)
            authorizations.append(self.headers.get("Authorization", ""))
            if len(requests) == 1:
                response = {
                    "choices": [{
                        "message": {
                            "content": None,
                            "tool_calls": [{
                                "id": "read-marker",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"marker.txt"}',
                                },
                            }],
                        },
                    }],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                }
            else:
                response = {
                    "choices": [{
                        "message": {
                            "content": requests[-1]["messages"][-1]["content"],
                        },
                    }],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(
            "AUTOFORM_AVOCADO_BASE_URL",
            f"http://127.0.0.1:{server.server_port}/v1",
        )
        monkeypatch.setenv("AUTOFORM_AVOCADO_MODEL", "loopback-avocado")
        monkeypatch.setenv("AUTOFORM_AVOCADO_KEY_VAR", "PILOT_TOKEN")
        monkeypatch.setenv("PILOT_TOKEN", "loopback-secret")
        result = provider_check.probe("avocado")
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()

    assert result == {
        "ok": True,
        "tool_calls": 1,
        "turns": 2,
        "provider": "avocado",
        "model": "loopback-avocado",
    }
    assert len(requests) == 2
    assert authorizations == ["Bearer loopback-secret", "Bearer loopback-secret"]

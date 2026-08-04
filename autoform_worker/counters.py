"""Attempt counters — per-worker budgets persisted across rounds.

Keys follow TauCetiWorker's shape: ``fix-{pr}-{head[:12]}``, ``ci-pr-{pr}``,
``prove-{node}``, ``review-err-{pr}``… A provider-infrastructure failure refunds
the attempt (bounded by an ``infra-…`` refund counter) so outages don't burn
real budgets.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .constants import MAX_INFRA_REFUNDS


class Counters:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=1, sort_keys=True))
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _as_count(value) -> int:
        # bool subclasses int — a hand-edited `true` must normalize to 0, not 1
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def get(self, key: str) -> int:
        return self._as_count(self._load().get(key, 0))

    def bump(self, key: str) -> int:
        data = self._load()
        value = self._as_count(data.get(key, 0)) + 1
        data[key] = value
        self._save(data)
        return value

    def refund(self, key: str) -> bool:
        """Undo one attempt after a provider-infra failure. Bounded — returns
        False once the refund budget for this key is spent."""
        data = self._load()
        refunds = data.get(f"infra-{key}", 0)
        if not isinstance(refunds, int) or refunds >= MAX_INFRA_REFUNDS:
            return False
        current = data.get(key, 0)
        if isinstance(current, int) and current > 0:
            data[key] = current - 1
        data[f"infra-{key}"] = (refunds if isinstance(refunds, int) else 0) + 1
        self._save(data)
        return True

    def clear(self, key: str) -> None:
        data = self._load()
        if key in data:
            del data[key]
            self._save(data)

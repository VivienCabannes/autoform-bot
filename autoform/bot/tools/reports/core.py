# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Report loading from a directory of JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ReportsLoader:
    """Loads task reports from a directory of JSON files."""

    def __init__(self, reports_path: Path) -> None:
        self._path = reports_path

    def load(self) -> list[dict[str, Any]]:
        """Read and parse all *.json files, sorted by name."""
        if not self._path.exists():
            return []
        reports: list[dict[str, Any]] = []
        for f in sorted(self._path.glob("*.json")):
            try:
                reports.append(json.loads(f.read_text()))
            except Exception:
                pass
        return reports

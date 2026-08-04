---
name: set-backend
description: Show or change Autoform's proof-worker backend and explain its authentication, billing, and data-egress implications.
---

Use `scripts/backend_config.py list|set` and report the selected adapter, credentials, billing path, and whether project data can leave the machine. Validate direct API backends with `scripts/provider_check.py`; configuration alone never grants workload-egress consent.

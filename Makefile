# Autoform — developer Makefile.
#
# Quick start:
#   make setup     # install uv + all Python deps (one time)
#   make demo      # scan the sample project — works with plain python3, no deps
#   make test      # run the test suite
#   make help      # list every target
#
# Most targets run through `uv`, which resolves dependencies from pyproject.toml
# on demand. `make demo` deliberately uses plain python3 so it works before any
# setup (the workspace scanner is pure stdlib).

SHELL       := /bin/bash
PYTHON      ?= python3
DEMO_DIR    ?= examples/demo-project
SERVERS     := repl lsp aristotle zulip

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@echo "Autoform — make targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

.PHONY: setup
setup: uv ## Install uv (if missing) and sync all Python deps
	uv sync --all-extras
	@echo ""
	@echo "Setup complete. Try:  make demo   or   make test"

.PHONY: uv
uv: ## Ensure uv is installed (official installer, then pip fallback)
	@command -v uv >/dev/null 2>&1 && { echo "uv $$(uv --version)"; exit 0; } || true
	@echo "uv not found — attempting install..."
	@curl -LsSf https://astral.sh/uv/install.sh | sh \
		|| $(PYTHON) -m pip install --user uv \
		|| { echo "Could not install uv automatically. See https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
	@echo "Installed. You may need to restart your shell so 'uv' is on PATH."

.PHONY: check
check: ## Run the full environment check (uv, deps, Lean, Zulip)
	bash skills/setup-autoform/setup-autoform.sh

# ---------------------------------------------------------------------------
# Play / run
# ---------------------------------------------------------------------------

.PHONY: demo
demo: ## Scan the sample Lean project (no deps required)
	@echo "== full scan =="
	@$(PYTHON) skills/workspace/inspect.py $(DEMO_DIR)
	@echo ""
	@echo "== declarations =="
	@$(PYTHON) skills/workspace/inspect.py --declarations $(DEMO_DIR)
	@echo ""
	@echo "== targets (the dependency DAG) =="
	@$(PYTHON) skills/workspace/inspect.py --targets $(DEMO_DIR)

.PHONY: zulip-status
zulip-status: ## Check whether Zulip credentials are configured
	uv run --extra zulip $(PYTHON) -c "from servers.zulip.server import create_zulip_server; import asyncio; print('zulip server constructs OK — configure ~/.zuliprc then use the MCP tools')"

# Launch a server on stdio for manual MCP testing, e.g. `make serve-zulip`.
.PHONY: $(addprefix serve-,$(SERVERS))
serve-zulip: ## Run the zulip MCP server on stdio
	uv run --extra zulip $(PYTHON) -m servers.zulip.server
serve-repl: ## Run the repl MCP server on stdio (stub)
	uv run --extra repl $(PYTHON) -m servers.repl.server
serve-lsp: ## Run the lsp MCP server on stdio (stub)
	uv run $(PYTHON) -m servers.lsp.server
serve-aristotle: ## Run the aristotle MCP server on stdio (stub)
	uv run --extra aristotle $(PYTHON) -m servers.aristotle.server

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

.PHONY: test
test: ## Run the test suite
	uv run --all-extras --with pytest pytest -q tests/

.PHONY: lint
lint: ## Run ruff over the Python sources
	uv run --with ruff ruff check servers/ skills/

.PHONY: lean
lean: ## Hint for setting up Lean 4 (needed for the repl/lsp servers)
	@command -v lean >/dev/null 2>&1 && lean --version \
		|| echo "Lean not configured. Run: elan default stable   (or use the /install-lean skill)"

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

.PHONY: clean
clean: ## Remove caches and the local virtualenv
	rm -rf .venv .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true

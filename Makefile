# Autoform — Makefile
#
# Two steps to play:
#   make setup           install Python deps (assistant-agnostic)
#   make install-claude  install the plugin into Claude Code
#                        → then launch `claude` and use /workspace, /zulip, /setup-autoform
#
# (Codex users: `make install-codex` instead.) Run `make help` for the list.

SHELL       := /bin/bash
PYTHON      ?= python3
CLAUDE      ?= claude
PLUGIN_DIR  := $(CURDIR)
PLUGIN      := autoform@autoform
MARKETPLACE := autoform

.DEFAULT_GOAL := help

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

# --- Setup (assistant-agnostic) ---------------------------------------------

.PHONY: setup
setup: ## Install Python deps (uv + project deps)
	@command -v uv >/dev/null 2>&1 || { echo "installing uv..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh || $(PYTHON) -m pip install --user uv; }
	uv sync --all-extras
	@echo "✅ Deps ready. Install the plugin:  make install-claude   (or  make install-codex)"

# --- Install the plugin into an assistant -----------------------------------

# Remove from Claude with:  claude plugin uninstall autoform@autoform
.PHONY: install-claude
install-claude: ## Install the plugin into Claude Code (user scope)
	@command -v $(CLAUDE) >/dev/null 2>&1 || { echo "! 'claude' CLI not found"; exit 1; }
	@$(CLAUDE) plugin marketplace add "$(PLUGIN_DIR)" 2>/dev/null \
		|| $(CLAUDE) plugin marketplace update $(MARKETPLACE) >/dev/null 2>&1 || true
	@$(CLAUDE) plugin install $(PLUGIN)
	@echo "✅ Installed — launch 'claude' and try /workspace, /zulip, /setup-autoform"

# Remove from Codex with:  codex plugin remove autoform@autoform-local
.PHONY: install-codex
install-codex: ## Install the plugin into Codex CLI (local marketplace)
	@command -v codex >/dev/null 2>&1 || { echo "! 'codex' CLI not found"; exit 1; }
	@set -e; root="$${CODEX_AUTOFORM_MARKETPLACE:-$$HOME/.autoform-codex-marketplace}"; \
		mkdir -p "$$root/plugins" "$$root/.agents/plugins"; \
		[ -L "$$root/plugins/autoform" ] && rm "$$root/plugins/autoform" || true; \
		ln -s "$(PLUGIN_DIR)" "$$root/plugins/autoform"; \
		printf '%s\n' '{"name":"autoform-local","interface":{"displayName":"AutoForm Local"},"plugins":[{"name":"autoform","source":{"source":"local","path":"./plugins/autoform"},"policy":{"installation":"AVAILABLE","authentication":"ON_INSTALL"},"category":"Coding"}]}' > "$$root/.agents/plugins/marketplace.json"; \
		codex plugin marketplace add "$$root" 2>/dev/null || true; \
		codex plugin add autoform@autoform-local

# --- Use & develop ----------------------------------------------------------

.PHONY: demo
demo: ## Scan the bundled sample Lean project (no deps)
	@$(PYTHON) skills/workspace/inspect.py examples/demo-project

.PHONY: test
test: ## Run the test suite
	uv run --all-extras --with pytest pytest -q tests/

.PHONY: lint
lint: ## Lint the Python sources (ruff)
	uv run --with ruff ruff check servers/ skills/

.PHONY: clean
clean: ## Remove .venv and caches
	rm -rf .venv .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# Autoform — Makefile
#
# Two steps to play:
#   make setup           install Python deps (assistant-agnostic)
#   make install-claude  install the plugin into Claude Code
#                        → then launch `claude` and use /autoform:setup
#
# (Codex users: `make install-codex`; Muse users: `make install-muse`.)
# Run `make help` for the list.

SHELL       := /bin/bash
PYTHON      ?= python3
CLAUDE      ?= claude
TBH         ?= tbh
PLUGIN_DIR  := $(CURDIR)
PLUGIN      := autoform@autoform
MARKETPLACE := autoform
MUSE_DIST   ?= $(CURDIR)/dist/muse/autoform

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
	@echo "✅ Deps ready. Install the plugin with make install-claude, install-codex, or install-muse"

# --- Install the plugin into an assistant -----------------------------------

# Remove from Claude with:  claude plugin uninstall autoform@autoform
.PHONY: install-claude
install-claude: ## Install the plugin into Claude Code (user scope)
	@command -v $(CLAUDE) >/dev/null 2>&1 || { echo "! 'claude' CLI not found"; exit 1; }
	@$(CLAUDE) plugin marketplace add "$(PLUGIN_DIR)" 2>/dev/null \
		|| $(CLAUDE) plugin marketplace update $(MARKETPLACE) >/dev/null 2>&1 || true
	@$(CLAUDE) plugin install $(PLUGIN)
	@echo "✅ Installed — launch 'claude' and try /autoform:setup"

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

.PHONY: build-muse
build-muse: ## Build a clean single-manifest Muse/TBH plugin package
	@$(PYTHON) scripts/build_muse_plugin.py --output "$(MUSE_DIST)" --force

.PHONY: validate-muse
validate-muse: build-muse ## Validate the staged plugin with the installed Muse/TBH CLI
	@command -v $(TBH) >/dev/null 2>&1 || { echo "! '$(TBH)' CLI not found"; exit 1; }
	@$(TBH) plugins validate "$(MUSE_DIST)" --json

.PHONY: install-muse
install-muse: validate-muse ## Install and enable the staged plugin in Muse/TBH
	@$(TBH) plugins install "$(MUSE_DIST)"
	@$(TBH) plugins enable autoform
	@echo "✅ Installed — launch '$(TBH)' and try /autoform:setup"

# --- Use & develop ----------------------------------------------------------

.PHONY: demo
demo: ## Scan the Lean workspace regression fixture (no deps)
	@$(PYTHON) scripts/workspace_inspector.py tests/fixtures/demo-project

.PHONY: test
test: ## Run the test suite
	uv run --all-extras --with pytest pytest -q tests/

.PHONY: lint
lint: ## Lint the Python sources (ruff)
	uv run --with ruff ruff check scripts/ servers/ autoform_worker/ tests/

.PHONY: clean
clean: ## Remove .venv and caches
	rm -rf .venv .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

.PHONY: setup venv deps lean elan workspace repl mathlib lsp ripgrep clean clean-lean help submodules freeze unfreeze

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
MATHLIB := submodules/mathlib
REPL := submodules/repl
LSP := submodules/lean-lsp-mcp
WORKSPACE := template
LAKE := PATH="$(HOME)/.elan/bin:$(PATH)" lake

help:
	@echo "make setup       — full setup (venv + deps + submodules + elan + lean)"
	@echo "make venv        — create Python virtualenv"
	@echo "make deps        — install Python dependencies"
	@echo "make elan        — install elan toolchain manager (provides lean + lake)"
	@echo "make workspace   — build Lean workspace (Mathlib cache + REPL)"
	@echo "make freeze      — make shared Lean artifacts read-only (run after workspace)"
	@echo "make unfreeze    — restore write permissions on shared Lean artifacts"
	@echo "make lean        — build workspace + install lean-lsp-mcp"
	@echo "make mathlib     — build Mathlib standalone (prefer make workspace)"
	@echo "make repl        — build REPL standalone (prefer make workspace)"
	@echo "make lsp         — install lean-lsp-mcp"
	@echo "make ripgrep     — install ripgrep via pip"
	@echo "make clean       — remove venv and build artifacts"
	@echo "make clean-lean  — nuke all Lean build artifacts and submodules"

# ---------- Full setup ----------

setup: unfreeze venv deps submodules elan lean ripgrep
	@echo "Setup complete."

# ---------- Python ----------

venv:
	@test -d $(VENV) || python3 -m venv $(VENV)
	@echo "Virtualenv ready: $(VENV)"

deps: venv
	$(PIP) install -e ".[dev,webapp]"

deps-webapp: venv
	$(PIP) install -e ".[webapp]"

# ---------- Submodules ----------

submodules:
	git submodule update --init --recursive

# ---------- Lean ----------

elan:
	@which elan > /dev/null 2>&1 || PATH="$(HOME)/.elan/bin:$(PATH)" which elan > /dev/null 2>&1 || { \
		echo "Installing elan (Lean toolchain manager)..."; \
		curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none; \
	}
	@echo "elan ready: $$(PATH="$(HOME)/.elan/bin:$(PATH)" elan --version)"

lean: workspace lsp freeze

workspace: elan submodules
	@echo "Building Lean workspace (this may take a while on first run)..."
	cd $(MATHLIB) && $(LAKE) exe cache get && $(LAKE) build
	cd $(WORKSPACE) && $(LAKE) exe cache get && $(LAKE) build repl
	@test -f $(REPL)/.lake/build/bin/repl || (echo "REPL binary not found" && exit 1)
	@echo "Workspace ready. REPL binary: $(REPL)/.lake/build/bin/repl"

freeze:
	@echo "Freezing shared Lean artifacts (read-only)..."
	chmod -R a-w submodules/mathlib/ submodules/repl/ template/.lake/
	@echo "Frozen. Concurrent lake builds cannot corrupt shared oleans."

unfreeze:
	@echo "Unfreezing shared Lean artifacts (restoring write)..."
	@for d in submodules/mathlib submodules/repl template/.lake; do \
	  [ -d "$$d" ] && chmod -R u+w "$$d" || true; \
	done
	@echo "Unfrozen. Run 'make freeze' after rebuilding."

mathlib: elan submodules
	@echo "Building Mathlib (this may take a while on first run)..."
	cd $(MATHLIB) && $(LAKE) exe cache get && $(LAKE) build
	@echo "Mathlib ready."

repl: elan submodules
	@echo "Building REPL..."
	cd $(REPL) && $(LAKE) build
	@test -f $(REPL)/.lake/build/bin/repl || (echo "REPL binary not found" && exit 1)
	@echo "REPL ready: $(REPL)/.lake/build/bin/repl"

lsp: venv submodules
	$(PIP) install -e $(LSP)
	@echo "lean-lsp-mcp installed."

# ---------- Ripgrep ----------

ripgrep: venv
	@which rg > /dev/null 2>&1 || $(PIP) install ripgrep
	@echo "ripgrep ready."

# ---------- Toolchain check ----------

check-toolchain:
	@echo "REPL toolchain: $$(cat $(REPL)/lean-toolchain)"
	@echo "Mathlib toolchain: $$(cat $(MATHLIB)/lean-toolchain)"
	@test "$$(cat $(REPL)/lean-toolchain)" = "$$(cat $(MATHLIB)/lean-toolchain)" \
		|| (echo "ERROR: toolchain mismatch!" && exit 1)
	@echo "Toolchains match."

# ---------- Clean ----------

clean-lean: unfreeze
	@echo "Removing Lean build caches and deinitializing submodules..."
	rm -rf $(WORKSPACE)/.lake
	rm -rf $(MATHLIB)/.lake
	rm -rf $(REPL)/.lake
	rm -rf $(LSP)/.lake
	git submodule deinit -f $(MATHLIB)
	git submodule deinit -f $(REPL)
	git submodule deinit -f $(LSP)
	@echo "Lean environment cleaned. Run 'make lean' to rebuild."

clean:
	@echo "This will remove .venv/ and output/. Continue? [y/N]" && read ans && [ "$$ans" = "y" ]
	rm -rf $(VENV)
	rm -rf output/
	@echo "Cleaned."

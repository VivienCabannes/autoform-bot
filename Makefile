.PHONY: setup test lint check-example

THESIS_EXAMPLE := skills/setup/assets/cabannes-thesis-project

setup:
	uv sync --extra dev --extra repl

test:
	uv run pytest -q

lint:
	uv run ruff check autoform_cli servers tests

check-example:
	uv run autoform check $(THESIS_EXAMPLE)/blueprint
	uv run autoform-visualize $(THESIS_EXAMPLE)/blueprint \
		--output $(THESIS_EXAMPLE)/blueprint/dependencies.html \
		--link-extension .html
	uv run --extra dev mkdocs build --strict --config-file $(THESIS_EXAMPLE)/mkdocs.yml

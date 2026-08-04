.PHONY: setup test lint check-example

setup:
	uv sync --extra dev --extra repl

test:
	uv run pytest -q

lint:
	uv run ruff check autoform_cli servers tests

check-example:
	uv run autoform check examples/blueprint

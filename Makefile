.PHONY: test lint format

test:
	uv run pytest -q

lint:
	uv run ruff check .

format:
	uv run ruff format .

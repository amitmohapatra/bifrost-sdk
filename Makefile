# The targets CI runs (.github/workflows/ci.yml), so a laptop and CI check the same things.
.PHONY: sync lint format-check test test-live examples links check

sync:  ## the package with its dev extras (pytest, respx, ruff, mcp)
	uv sync --all-extras

lint:
	uv run ruff check .

format-check:
	uv run ruff format --check .

test:  ## unit tests: the gateway is mocked, nothing touches the network
	uv run pytest -q

test-live:  ## against a running gateway: BIFROST_URL (see README, Development)
	uv run pytest -q -m live

examples:  ## run every numbered example against the in-process fake gateway (offline)
	@for f in examples/[0-9]*.py; do \
		echo "== $$f"; uv run python "$$f" || exit 1; \
	done

links:  ## fail on a broken relative link or anchor in any Markdown file
	python3 scripts/check_links.py .

check: lint format-check test examples links

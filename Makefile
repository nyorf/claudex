UV ?= uv

.PHONY: install run fmt lint test prose check context-smoke cli-smoke serve-smoke workflow-smoke wheel bundle

install:
	$(UV) sync --locked

run:
	$(UV) run --locked claudex run

fmt:
	$(UV) run --locked ruff check --fix src tests scripts
	$(UV) run --locked ruff format src tests scripts

lint:
	$(UV) run --locked ruff check src tests scripts
	$(UV) run --locked ruff format --check src tests scripts
	$(UV) run --locked pyright
	$(UV) run --locked bandit -q -r src -c pyproject.toml
	$(UV) run --locked lint-imports

test:
	$(UV) run --locked pytest $(PYTEST_ARGS)

prose:
	node scripts/prose-gate.mjs
	node --test scripts/prose-gate.test.mjs

check: lint prose test

context-smoke:
	$(UV) run --locked python scripts/context_smoke.py

cli-smoke:
	$(UV) run --locked python scripts/cli_smoke.py

serve-smoke:
	$(UV) run --locked python scripts/serve_smoke.py

workflow-smoke:
	$(UV) run --locked python scripts/workflow_smoke.py

wheel:
	$(UV) lock --check
	$(UV) build

bundle:
	$(UV) run --locked --group bundle python scripts/bundle.py

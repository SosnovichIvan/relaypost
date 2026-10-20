PYTHON ?= .venv/bin/python

.PHONY: check test run e2e
check:
	$(PYTHON) -m ruff format --check relaypost tests scripts
	$(PYTHON) -m ruff check relaypost tests scripts
	$(PYTHON) -m mypy relaypost
	$(MAKE) test

test:
	$(PYTHON) -m coverage run -m pytest
	$(PYTHON) -m coverage report
	$(PYTHON) -m coverage json -o coverage.json
	$(PYTHON) scripts/check_coverage.py

run:
	$(PYTHON) -m relaypost

e2e:
	npm run --prefix web-tests e2e

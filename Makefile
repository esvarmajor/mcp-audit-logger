.PHONY: help install test lint type fmt smoke run clean ci

PYTHON ?= python3
VENV   ?= .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
RUFF   := $(VENV)/bin/ruff
MYPY   := $(VENV)/bin/mypy

help:
	@echo "Targets:"
	@echo "  install    Create venv (if missing) and install in editable mode with dev extras"
	@echo "  test       Run pytest"
	@echo "  lint       Run ruff check"
	@echo "  type       Run mypy on src/"
	@echo "  fmt        Run ruff format"
	@echo "  smoke      End-to-end smoke test (boots proxy, round-trips an MCP call)"
	@echo "  run        Start the proxy on 127.0.0.1:8765 with no downstream"
	@echo "  ci         Run lint + type + test (the full CI matrix)"
	@echo "  clean      Delete venv, build artifacts, and cache directories"

$(VENV)/bin/python:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e '.[dev]'

install: $(VENV)/bin/python

test: install
	$(PYTEST) -q

lint: install
	$(RUFF) check src tests

fmt: install
	$(RUFF) check --fix src tests
	$(RUFF) format src tests

type: install
	$(MYPY) src/mcp_audit_logger --ignore-missing-imports

smoke: install
	$(PY) scripts/smoke.py

run: install
	$(PY) -m mcp_audit_logger --host 127.0.0.1 --port 8765

ci: lint type test

clean:
	rm -rf $(VENV) build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} +

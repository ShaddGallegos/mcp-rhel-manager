.PHONY: format lint fix mic-check
.PHONY: systemd-tokenize systemd-install

format:
	./scripts/format.sh

lint:
	@echo "Running ruff, black --check, shellcheck, shfmt"
	ruff check . || true
	black --check . || true
	shfmt_files="$(find scripts -type f -name '*.sh' -print0 | xargs -0 echo)" || true; \
	if [ -n "$$shfmt_files" ]; then shfmt -l -i 2 -s $$shfmt_files || true; fi
	if command -v shellcheck >/dev/null 2>&1; then \
		find scripts -type f -name '*.sh' -print0 | xargs -0 -n1 shellcheck -x || true; \
	else \
		echo "shellcheck not available"; \
	fi

fix:
	pre-commit run --all-files

mic-check:
	bash -n scripts/auto-fixer.sh && echo 'syntax OK' || echo 'syntax ERR'

systemd-tokenize:
	@echo "Tokenizing systemd unit files into packaging/systemd-templates"
	@./scripts/render_systemd_units.sh --src packaging/systemd --dest packaging/systemd-templates || true

systemd-install:
	@echo "Rendering tokenized units and installing to /etc/systemd/system (requires sudo)"
	sudo ./scripts/render_systemd_units.sh --src packaging/systemd-templates --dest packaging/systemd-rendered --apply --install --enable || true
SHELL := /usr/bin/env bash

PYTHON ?= python3
VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help venv install install-dev precommit-install precommit-run fmt lint clean-venv

help:
	@echo "Makefile targets:"
	@echo "  make venv            # create virtualenv"
	@echo "  make install         # install runtime requirements"
	@echo "  make install-dev     # install dev requirements"
	@echo "  make precommit-install # install git pre-commit hooks"
	@echo "  make precommit-run   # run pre-commit against all files"
	@echo "  make fmt             # run black"
	@echo "  make lint            # run flake8"
	@echo "  make clean-venv      # remove .venv"

venv:
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	@$(PY) -m pip install --upgrade pip setuptools wheel

install: venv
	@if [ -f requirements.txt ]; then $(PIP) install -r requirements.txt; else echo "no requirements.txt to install"; fi

install-dev: install
	@if [ -f dev-requirements.txt ]; then $(PIP) install -r dev-requirements.txt; else echo "no dev-requirements.txt to install"; fi

precommit-install: install-dev
	@echo "Installing pre-commit git hook..."
	@$(VENV)/bin/pre-commit install || true

precommit-run: install-dev
	@echo "Running pre-commit against all files..."
	@$(VENV)/bin/pre-commit run --all-files

fmt:
	@$(VENV)/bin/black .

lint:
	@$(VENV)/bin/flake8 .

clean-venv:
	@rm -rf $(VENV)

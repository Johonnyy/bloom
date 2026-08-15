# Shortcuts for the things you do more than once.
#
# Everything here is a thin wrapper over a command you could type yourself; the
# point is that the flags are remembered, not hidden. `make help` lists targets.

COMPOSE     := docker compose
COMPOSE_ALL := docker compose -f docker-compose.yml -f docker-compose.prod.yml
# Windows puts the venv's executables in Scripts/, POSIX in bin/. This repo is
# developed on both, and a hardcoded path means `make test` works for exactly one
# of them.
PY          := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo .venv/Scripts/python)
PIP         := $(PY) -m pip

.DEFAULT_GOAL := help
.PHONY: help venv install dev up down logs shell test lint fmt openapi build prod-config clean

help:  ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv:  ## Create the local virtualenv
	python3 -m venv .venv

install: venv  ## Install runtime + dev dependencies, and seed .env
	$(PIP) install -e ".[dev]"
	@test -f .env || cp .env.example .env

dev: up  ## Alias for `up`

up:  ## Start the app with hot reload
	$(COMPOSE) up --build -d
	@echo "http://localhost:8010/health"

down:  ## Stop the stack (volumes survive; add -v to drop them)
	$(COMPOSE) down

logs:  ## Follow the app's logs
	$(COMPOSE) logs -f app

shell:  ## A shell inside the running app container
	$(COMPOSE) exec app bash

test:  ## Run the test suite (needs nothing running)
	$(PY) -m pytest

lint:  ## Check formatting and lint rules
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:  ## Apply formatting and autofixable lint rules
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

openapi:  ## Regenerate docs/openapi.json — the schema Aperture codegens from
	$(PY) -m app.openapi > docs/openapi.json
	@echo "docs/openapi.json updated. CI fails if this is stale."

build:  ## Build the production image
	docker build -f docker/Dockerfile -t bloom:local .

prod-config:  ## Render the merged production compose config without starting anything
	$(COMPOSE_ALL) config

clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

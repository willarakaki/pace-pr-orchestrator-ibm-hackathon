# ─────────────────────────────────────────────────────────────────────────────
# PACE PR Orchestrator — Developer Makefile
#
# Targets:
#   make run      — start the orchestrator locally (requires a populated .env)
#   make test     — run the full pytest suite with coverage
#   make build    — build the production Docker image
#   make lint     — run ruff for linting + mypy for type-checking
#   make clean    — remove __pycache__ / .pyc artefacts
# ─────────────────────────────────────────────────────────────────────────────

IMAGE_NAME   ?= pace-pr-orchestrator
IMAGE_TAG    ?= latest
PYTHON       ?= python
PIP          ?= pip
SRC_DIR      := src
TEST_DIR     := tests

.PHONY: run test build lint clean help

## ── Default target ───────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  PACE PR Orchestrator — available make targets"
	@echo ""
	@echo "  run     Start the service locally (reads .env if present)"
	@echo "  test    Run pytest suite with coverage report"
	@echo "  build   Build the Docker image ($(IMAGE_NAME):$(IMAGE_TAG))"
	@echo "  lint    Run ruff + mypy static analysis"
	@echo "  clean   Remove __pycache__ and compiled artefacts"
	@echo ""

## ── Local run ────────────────────────────────────────────────────────────────
run:
	@if [ -f .env ]; then \
	    export $$(grep -v '^#' .env | xargs) && $(PYTHON) -m $(SRC_DIR).main; \
	else \
	    $(PYTHON) -m $(SRC_DIR).main; \
	fi

## ── Tests ────────────────────────────────────────────────────────────────────
test:
	$(PYTHON) -m pytest $(TEST_DIR) \
	    --asyncio-mode=auto \
	    --tb=short \
	    -v \
	    --cov=$(SRC_DIR) \
	    --cov-report=term-missing \
	    --cov-report=html:htmlcov

## ── Docker build ─────────────────────────────────────────────────────────────
build:
	docker build \
	    --tag $(IMAGE_NAME):$(IMAGE_TAG) \
	    --file Dockerfile \
	    .

## ── Lint ─────────────────────────────────────────────────────────────────────
lint:
	@echo "── ruff ──────────────────────────────────────────────────────────────"
	$(PYTHON) -m ruff check $(SRC_DIR) $(TEST_DIR)
	@echo "── mypy ──────────────────────────────────────────────────────────────"
	$(PYTHON) -m mypy $(SRC_DIR) --ignore-missing-imports

## ── Clean ────────────────────────────────────────────────────────────────────
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -rf htmlcov .coverage .mypy_cache .ruff_cache

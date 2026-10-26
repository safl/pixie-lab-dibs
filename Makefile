# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause
#
# Common tasks, and the home of the CI logic: the workflows call these
# targets, so everything CI does can be run locally. Run "make" for the list.

UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help format lint typecheck test build check

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  %-10s %s\n", $$1, $$2}'

format: ## Run every pre-commit hook: ruff, mdformat, reuse, ASCII punctuation
	pre-commit run --all-files

lint: ## ruff check
	$(UV) run ruff check .

typecheck: ## mypy --strict over src
	$(UV) run mypy

test: ## pytest
	$(UV) run pytest -q

build: ## Build the sdist and wheel, and check every command the wheel installs runs
	rm -rf dist
	$(UV) build
	for cmd in $$(unzip -p dist/*.whl '*/entry_points.txt' | sed -n 's/ *=.*//p' | grep -v '^\['); do \
		$(UV) tool run --from "$$(ls dist/*.whl)" "$$cmd" --help >/dev/null || exit 1; \
	done

check: lint typecheck test ## Everything but format

# stapel-search — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its own contract triad (schema.json + flows.json +
# errors.json) from a single-module {search + core} Django instance mounted
# at the canonical /search/api/v1 prefix (see _codegen.py /
# _codegen_settings.py / codegen_urls.py), plus TWO artifacts that are its
# own and are generated from the code implementing them:
#
#   docs/index.json    INDEX_FIELDS — every indexed field's source, read
#                      paths and proving test (spec §11 layer 1)
#   docs/ranking.json  the P2B Art. 5 ranking disclosure, rendered from the
#                      scorer registry that does the ranking (spec §10)
#
# Both are under the same drift gate as the quintet, which is the whole
# point: a compliance text generated from the code cannot disagree with the
# behaviour, and an index field cannot be added without its contract row.
#
# PYTHON must have the module + its deps importable (the repo venv, or a CI
# venv). Emission is pinned to Python 3.12: drf-spectacular renders
# component descriptions differently across minors, and a contract emitted
# on the wrong one produces false diffs forever.
PYTHON ?= python3

.PHONY: contract contract-check migration-lint lint test emit-check index-lint

# Emit the triad + index.json + ranking.json + capabilities.json + llms.txt,
# then assemble README.md from docs/readme.md plus everything above.
#
# The llms.txt budget is raised from the generator's default 4000 to 5000 —
# the same deliberate exception stapel-forms (5000), stapel-recordings
# (5000) and stapel-workspaces (4500) already take. The measured document is
# ~4570 tokens and the bulk of it is the 39-entry usage surface: four merge
# registries with symmetric register/unregister/get triples, plus the
# indexing verbs. Raise the ceiling on purpose; do NOT shorten the `intent`
# lines to fit, because a trimmed context file reads exactly like a complete
# one at the point of use, which is the failure the hard budget exists to
# prevent.
contract:
	$(PYTHON) -m stapel_search._codegen --out docs
	$(PYTHON) -m stapel_search._capabilities --out docs
	$(PYTHON) -m stapel_tools.llms_txt . --out docs --budget 5000
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: regenerate into a temp dir and diff against the committed docs/*.
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_search._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	cp docs/capabilities.meta.json "$$tmp/" 2>/dev/null || true; \
	$(PYTHON) -m stapel_search._capabilities --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	$(PYTHON) -m stapel_tools.llms_txt . --out "$$tmp" --budget 5000 || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json index.json ranking.json capabilities.json llms.txt; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.readme . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/* + README.md up to date"; fi; \
	exit $$rc

# Expand/contract gate for Django migrations (release-management.md §3).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict $(if $(BASE_SHA),--base-sha $(BASE_SHA),)

# Outbox discipline: an emit outside its mutating transaction is a row that
# exists without the fact it announced.
emit-check:
	$(PYTHON) -m stapel_core.lint.emit_check .

# The fleet gate this module contributed: declared index field => reachable
# read path => proving test.
index-lint:
	$(PYTHON) -m stapel_tools.index_lint . --strict

lint:
	ruff check . --select E,F,W --ignore E501

test:
	$(PYTHON) -m pytest tests/ -q

# The Postgres and Meilisearch suites need real servers — behaviour that
# cannot be faked is tested for real or skipped, never simulated. These
# targets spin throwaway containers (OrbStack/Docker), run the suite and
# tear them down.
.PHONY: test-postgres test-meili containers-up containers-down

PG_PORT ?= 55433
MEILI_PORT ?= 57700

containers-up:
	-docker rm -f stapel-search-pg stapel-search-meili 2>/dev/null
	docker run -d --name stapel-search-pg -e POSTGRES_PASSWORD=stapel \
		-e POSTGRES_DB=search_test -p $(PG_PORT):5432 postgres:16
	docker run -d --name stapel-search-meili -e MEILI_MASTER_KEY=stapel-test-key \
		-e MEILI_NO_ANALYTICS=true -p $(MEILI_PORT):7700 getmeili/meilisearch:v1.11
	@echo "waiting for postgres..."
	@until docker exec stapel-search-pg psql -U postgres -d search_test -tAc 'select 1' >/dev/null 2>&1; do :; done
	@echo "waiting for meilisearch..."
	@until curl -fsS http://127.0.0.1:$(MEILI_PORT)/health >/dev/null 2>&1; do :; done
	@echo "ready"

containers-down:
	-docker rm -f stapel-search-pg stapel-search-meili

test-postgres:
	STAPEL_SEARCH_TEST_DB="postgres://postgres:stapel@127.0.0.1:$(PG_PORT)/search_test" \
		$(PYTHON) -m pytest tests/ -q

test-meili:
	STAPEL_SEARCH_MEILI_URL="http://127.0.0.1:$(MEILI_PORT)" \
	STAPEL_SEARCH_MEILI_KEY="stapel-test-key" \
		$(PYTHON) -m pytest tests/test_backend_conformance.py -q -k meili

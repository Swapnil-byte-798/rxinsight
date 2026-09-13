# RxInsight — pharmaceutical commercial analytics warehouse
PY := .venv/bin/python
PIP := .venv/bin/pip
PSQL := docker exec -i rxinsight-db psql -U rxinsight -d rxinsight
DSN := postgresql://rxinsight:rxinsight@localhost:5544/rxinsight

.PHONY: help venv up down load generate report test tune clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

venv: ## create the virtualenv and install dependencies
	python3 -m venv .venv && $(PIP) install -q -r requirements.txt

up: ## start Postgres and wait for it to be healthy
	docker compose up -d
	@until docker exec rxinsight-db pg_isready -U rxinsight -d rxinsight >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready on :5544"

down: ## stop Postgres (data volume survives)
	docker compose down

generate: ## generate the synthetic source extracts
	$(PY) -m etl.generate_data

load: ## run the full ETL pipeline (schema, COPY, transform, load)
	$(PY) -m etl.pipeline

report: ## run the four analytics queries
	@for q in sql/analytics/*.sql; do \
		echo "\n=== $$q ==="; $(PSQL) -f /dev/stdin < $$q; \
	done

tune: ## measure the flagship report before and after indexing
	$(PY) -m etl.measure

test: ## run the test suite
	.venv/bin/pytest tests -q

clean: ## remove generated CSVs and caches
	rm -f data/*.csv; find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

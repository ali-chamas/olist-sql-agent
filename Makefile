.PHONY: setup db test check-data lint format serve eval eval-selftest

setup:        ## install the package with dev tools
	pip install -e ".[dev]"

db:           ## download the workbook and build data/olist.db
	python scripts/build_db.py --download

test:         ## unit tests (no API key, no real data needed)
	pytest -q
	node --test tests/js/*.test.mjs

check-data:   ## verify the database against the assumptions the prompt relies on
	python -m sqlagent check-data

lint:
	ruff check . && ruff format --check .

format:
	ruff format .

serve:        ## web app on http://127.0.0.1:8000
	python -m sqlagent serve

eval:         ## run the 50 reference questions; TPM=8000 suits Groq's free plan
	python -m sqlagent eval --tpm $(or $(TPM),8000)

eval-selftest: ## check the harness with the gold SQL; must print 50/50
	python -m sqlagent eval --oracle

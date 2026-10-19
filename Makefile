PYTHON ?= python3

generate:
	$(PYTHON) scripts/generate.py

up:
	docker compose up --build -d

test:
	docker compose exec api pytest -q

logs:
	docker compose logs -f api

check:
	bash scripts/check.sh

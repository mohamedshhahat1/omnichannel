.PHONY: up down build logs migrate test test-integration lint format-check typecheck compile check

up:
	docker compose up --build

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f api worker beat

migrate:
	docker compose run --rm migrate

test:
	cd backend && pytest -m 'not integration'

test-integration:
	cd backend && pytest -m integration

lint:
	cd backend && ruff check .

format-check:
	cd backend && ruff format --check .

typecheck:
	cd backend && mypy

compile:
	python -m compileall -q backend/app backend/tests backend/alembic

check: compile lint format-check typecheck test

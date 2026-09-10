.PHONY: test install run probe docker-build docker-up

install:
	python3 -m venv .venv
	./.venv/bin/pip install -r requirements-dev.txt

test:
	./.venv/bin/python -m pytest

run:
	AMBIWLED_CONFIG_DIR=./config ./.venv/bin/python -m ambiwled

probe:
	./probe.py $(IP)

docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

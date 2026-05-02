.PHONY: install lint fmt test

install:
	pip install ruff pytest

lint:
	ruff check .

fmt:
	ruff format .

test:
	pytest

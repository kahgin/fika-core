VENV := .venv

all: sync

venv:
	@uv venv $(VENV) --clear

sync: venv
	@uv lock
	@uv sync --frozen

sync-prod: venv
	@uv lock
	@uv sync --frozen --no-dev

update: venv
	@uv lock --upgrade
	@uv sync --frozen

dev:
	@uv run uvicorn app.main:app --reload --port 8000

run:
	@uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

test:
	@uv run pytest 

clean:
	@rm -rf build *.egg-info .pytest_cache .ruff_cache
	@find app tests -type d -name "__pycache__" -exec rm -rf {} +

distclean: clean
	@rm -rf $(VENV) uv.lock

.PHONY: all venv sync sync-prod update dev run clean distclean

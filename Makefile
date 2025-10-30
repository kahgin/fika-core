VENV = .venv

all: install

venv: 
	@uv venv $(VENV) --clear

install: venv
	@uv pip install .

upgrade: venv
	@uv pip install --upgrade .

clean:
	@rm -rf build *.egg-info
	@find app -type d -name "__pycache__" -exec rm -rf {} +

dev:
	@uv run uvicorn app.main:app --reload --port 8000

run:
	@uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

.PHONY: all venv install upgrade clean dev run

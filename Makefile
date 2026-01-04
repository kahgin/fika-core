VENV := .venv

-include .env
export

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
	@rm -rf .ruff_cache .pytest_cache
	@find app tests -type d -name "__pycache__" -exec rm -rf {} +

distclean: clean
	@rm -rf $(VENV) uv.lock

# VM Commands

vm-start:
	@az vm start --resource-group $(VM_RG) --name $(VM_NAME)

vm-stop:
	@az vm deallocate --resource-group $(VM_RG) --name $(VM_NAME)

vm-ssh:
	@ssh -i $(VM_KEY) $(VM_USER)@$(VM_IP)

vm-logs:
	@ssh -i $(VM_KEY) $(VM_USER)@$(VM_IP) 'cd $(VM_PROJECT_DIR) && docker-compose logs -f core'

vm-sync-env:
	@grep -v '^VM_' .env | sed 's|OSRM_URL=.*|OSRM_URL=http://osrm:5000|' > .env.vm.tmp
	@scp -i $(VM_KEY) .env.vm.tmp $(VM_USER)@$(VM_IP):$(VM_PROJECT_DIR)/fika-core/.env
	@rm .env.vm.tmp

vm-deploy:
	@ssh -i $(VM_KEY) $(VM_USER)@$(VM_IP) '\
		cd $(VM_PROJECT_DIR)/fika-core && git pull origin main && \
		cd $(VM_PROJECT_DIR) && docker-compose up -d --build core'

vm-deploy-full: vm-sync-env vm-deploy

.PHONY: all venv sync sync-prod update dev run test clean distclean \
	vm-start vm-stop vm-ssh vm-logs vm-sync-env vm-deploy vm-deploy-full
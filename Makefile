VENV := .venv
VM_RG := fika-rg
VM_NAME := fika-vm
VM_USER := azureuser
VM_IP := 4.218.15.39
VM_KEY := ~/.ssh/fika-vm_key.pem
VM_PROJECT_DIR := ~/fika

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

# Azure VM Control
vm-start:
	@echo "Starting Azure VM..."
	@az vm start --resource-group $(VM_RG) --name $(VM_NAME)
	@echo "VM started successfully!"

vm-stop:
	@echo "Stopping Azure VM (deallocating to stop billing)..."
	@az vm deallocate --resource-group $(VM_RG) --name $(VM_NAME)
	@echo "VM stopped successfully!"

vm-status:
	@az vm get-instance-view \
		--resource-group $(VM_RG) \
		--name $(VM_NAME) \
		--query "instanceView.statuses[1].{Status:code, DisplayStatus:displayStatus}" \
		--output table

vm-restart:
	@echo "Restarting Azure VM..."
	@az vm restart --resource-group $(VM_RG) --name $(VM_NAME)
	@echo "VM restarted successfully!"

# Deploy to VM
vm-deploy:
	@echo "Deploying to Azure VM..."
	@ssh -i $(VM_KEY) $(VM_USER)@$(VM_IP) '\
		cd $(VM_PROJECT_DIR)/fika-core && \
		git pull origin main && \
		cd $(VM_PROJECT_DIR) && \
		docker-compose up -d --build core'
	@echo "Deployment completed!"

# SSH into VM
vm-ssh:
	@ssh -i $(VM_KEY) $(VM_USER)@$(VM_IP)

# Full workflow: start VM, deploy, check status
vm-up: vm-start
	@echo "Waiting for VM to fully start (30 seconds)..."
	@sleep 30
	@$(MAKE) vm-deploy
	@$(MAKE) vm-status

# Stop VM after ensuring deployment is done
vm-down: vm-stop

# View VM logs
vm-logs:
	@ssh -i $(VM_KEY) $(VM_USER)@$(VM_IP) '\
		cd $(VM_PROJECT_DIR) && \
		docker-compose logs -f core'

# Emergency: force stop VM
vm-force-stop:
	@echo "Force stopping VM..."
	@az vm stop --resource-group $(VM_RG) --name $(VM_NAME) --force
	@az vm deallocate --resource-group $(VM_RG) --name $(VM_NAME)

optuna:
	@python -m app.services.acs_tuning_optuna --synthetic --trials 200

.PHONY: all venv sync sync-prod update dev run test clean distclean \
	vm-start vm-stop vm-status vm-restart vm-deploy vm-ssh vm-up vm-down \
	vm-logs vm-force-stop
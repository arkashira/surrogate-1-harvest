# surrogate-1-harvest — Makefile (one-command local dev)
.PHONY: help install dev test deploy rag-build status logs

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-15s %s\n", $$1, $$2}'

install: ## Install Python deps
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

dev: install ## Set up local dev environment + start daemons (caution: needs GCP creds)
	@echo "→ checking ~/.hermes/.env (canonical secret store)..."
	@[ -f ~/.hermes/.env ] || (echo "  ✗ ~/.hermes/.env missing — see docs/secret-store.md" && exit 1)
	@echo "→ checking gcloud auth..."
	@gcloud auth list --format="value(account)" | head -1
	@echo "→ ready. To run a daemon locally:"
	@echo "    set -a; source ~/.hermes/.env; set +a"
	@echo "    REPO_ROOT=$$PWD .venv/bin/python bin/axentx-research-daemon.py"

test: ## Run tests
	[ -d tests ] && .venv/bin/pytest tests/ || echo "  no tests/ dir"

deploy: ## Deploy bin/* to GCP
	@bash deploy.sh 2>/dev/null || echo "  deploy.sh missing — use direct gcloud compute scp"

rag-build: ## Rebuild RAG index from corpus
	REPO_ROOT=$$PWD .venv/bin/python bin/rag-build.py

status: ## Show daemon status on GCP
	gcloud compute ssh surrogate-watchdog --zone=us-central1-a --command='sudo systemctl list-units --type=service --state=running 2>&1 | grep -E "axentx|hermes|surrogate" | head -30'

logs: ## Tail recent logs
	gcloud compute ssh surrogate-watchdog --zone=us-central1-a --command='sudo journalctl -u "axentx-*" -u "hermes-*" --since "5 minutes ago" --no-pager | tail -50'

watchdog-test: ## Manually trigger watchdog
	gcloud compute ssh surrogate-watchdog --zone=us-central1-a --command='sudo systemctl restart surrogate-watchdog'

clean: ## Clean local artifacts
	rm -rf .venv __pycache__ */__pycache__ *.pyc

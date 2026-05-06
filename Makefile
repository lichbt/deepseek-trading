.PHONY: help setup verify validate live clean db-init db-reset test auto-research telegram healthcheck healthcheck

help:
	@echo "Trading Strategy Pipeline - Makefile Commands"
	@echo ""
	@echo "Setup & Verification:"
	@echo "  make setup        - Install dependencies and initialize database"
	@echo "  make verify       - Run setup verification"
	@echo ""
	@echo "Core Operations:"
	@echo "  make validate STRATEGY=<file.json>  - Validate strategy candidate"
	@echo "  make live ID=<strategy_id>          - Run live paper trader"
	@echo ""
	@echo "Auto Research:"
	@echo "  make auto-research TARGET=3 INST=EUR_USD  - Auto generate + validate"
	@echo ""
	@echo "Telegram Bot:"
	@echo "  make telegram     - Start Telegram notification bot (long polling)"
	@echo ""
	@echo "Testing:"
	@echo "  make test         - Run full test suite"
	@echo ""
	@echo "Database:"
	@echo "  make db-init      - Initialize database (creates tables)"
	@echo "  make db-reset     - Reset database (WARNING: deletes all data)"
	@echo ""
	@echo "Utilities:"
	@echo "  make healthcheck  - Run pipeline health check"
	@echo "  make clean        - Remove Python cache, __pycache__, .db"
	@echo ""
	@echo "Example:"
	@echo "  make setup"
	@echo "  make verify"
	@echo "  make test"
	@echo "  make auto-research TARGET=3 INST=EUR_USD"
	@echo "  make telegram"

setup:
	@echo "Setting up environment..."
	python3 -m venv venv
	. venv/bin/activate && pip install --upgrade pip
	. venv/bin/activate && pip install -r requirements.txt
	python setup_verify.py

verify:
	@echo "Running setup verification..."
	python setup_verify.py

validate:
	@if [ -z "$(STRATEGY)" ]; then \
		echo "Usage: make validate STRATEGY=<strategy_file.json>"; \
		exit 1; \
	fi
	USE_HISTORICAL_SPREADS=1 python3 validator.py $(STRATEGY)

live:
	@if [ -z "$(ID)" ]; then \
		echo "Usage: make live ID=<strategy_id>"; \
		exit 1; \
	fi
	python3 live_test.py $(ID)

auto-research:
	@if [ -z "$(TARGET)" ]; then \
		echo "Usage: make auto-research TARGET=3 INST=EUR_USD"; \
		exit 1; \
	fi
	USE_HISTORICAL_SPREADS=1 python3 auto_research.py --target $(or $(TARGET),1) --max-iter $(or $(MAX_ITER),30) --model deepseek/deepseek-chat-v3

test:
	@echo "Running tests..."
	python3 -m pytest tests/ -v -n 4

db-init:
	@echo "Initializing database..."
	python3 -c "from pipeline_utils import init_db; init_db(); print('Database ready')"

db-reset:
	@echo "WARNING: This will delete all strategy and validation data!"
	@read -p "Continue? [y/N] " -n 1 -r; \
	echo; \
	if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
		rm -f pipeline.db; \
		python3 -c "from pipeline_utils import init_db; init_db(); print('Database reset')"; \
	else \
		echo "Cancelled"; \
	fi

telegram:
	@echo "Starting Telegram bot..."
	python3 telegram_bot.py

healthcheck:
	@echo "Running healthcheck..."
	USE_LIVE_PRICING=1 python3 healthcheck.py

clean:
	@echo "Cleaning up..."
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name ".DS_Store" -delete
	rm -rf .auto-research-candidates
	@echo "Done"

.SILENT: help

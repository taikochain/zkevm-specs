help: ## Display this help screen
	@grep -h \
		-E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

install: # Install the Python packages
	pip3 install .[test,lint]

fmt: ## Format the code
	black .

lint: ## Check whether the code is formatted correctly
	black . --check
	flake8 .

type: ## Check the typing of the Python code
	mypy .

test: ## Run tests
	pytest --doctest-modules

test-eip1559:
	pytest tests/evm/test_end_tx.py

.PHONY: help install fmt lint test

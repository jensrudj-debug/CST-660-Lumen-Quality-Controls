PYTHON ?= python

.PHONY: all generate stage test validate

all: generate stage test validate

generate:
	$(PYTHON) generator/generate_data.py

stage:
	$(PYTHON) scripts/stage.py

test:
	$(PYTHON) -m pytest tests -v

validate:
	$(PYTHON) scripts/validate_contract.py contracts/accounts.yaml

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
	$(PYTHON) scripts/validate_contract.py --contract contracts/accounts.contract.yml --data data/staged/accounts.csv

.PHONY: help test doctor phase0 ingest-fixtures clean

help:
	@echo "make test              run the suite"
	@echo "make doctor            validate config, schema, guardrails, environment"
	@echo "make ingest-fixtures   load the fixture corpus into a fresh store"
	@echo "make phase0            ingest fixtures then run every Phase 0 report"
	@echo "make clean             remove the local store and generated reports"

test:
	python3 -m unittest discover -s tests -t . -v

doctor:
	./cb doctor

ingest-fixtures:
	rm -rf var/ledger.db
	./cb ingest mbox tests/fixtures/mail --mailbox quotes@civicbridges.com

phase0: ingest-fixtures
	./cb phase0

clean:
	rm -rf var __pycache__ .killswitch
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

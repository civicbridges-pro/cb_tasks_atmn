.PHONY: help test doctor selfcheck compliance phase0 phase1 ingest-fixtures clean

help:
	@echo "make test              run the suite"
	@echo "make doctor            validate config, schema, guardrails, environment"
	@echo "make selfcheck         data-quality checks on whatever mail is captured"
	@echo "make compliance        the compliance calendar; needs no mail at all"
	@echo "make ingest-fixtures   load the fixture corpus into a fresh store"
	@echo "make phase0            ingest fixtures then run every Phase 0 report"
	@echo "make phase1            preview the whole Phase 1 ledger loop, writing nothing"
	@echo "make clean             remove the local store and generated reports"

test:
	python3 -m unittest discover -s tests -t . -v

doctor:
	./cb doctor

selfcheck:
	./cb selfcheck

# The one target that works on a fresh checkout with no mail ingested.
compliance:
	-./cb compliance

ingest-fixtures:
	rm -rf var/ledger.db
	./cb ingest mbox tests/fixtures/mail --mailbox quotes@civicbridges.com

phase0: ingest-fixtures
	./cb phase0

# Preview only. Persisting to the ledger needs phase 1 declared in config/guardrails.yaml.
# The leading dash is deliberate: the fixture corpus contains deliberately overdue and
# unowned obligations, so a clean preview run still reports findings.
phase1: ingest-fixtures
	-./cb phase1

clean:
	rm -rf var __pycache__ .killswitch
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

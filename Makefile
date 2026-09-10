.PHONY: docs-prepare docs-serve docs-build

PYTHON ?= python

docs-prepare:
	cp openapi.yaml docs/openapi.yaml

docs-serve: docs-prepare
	$(PYTHON) -m mkdocs serve

docs-build: docs-prepare
	$(PYTHON) -m mkdocs build --strict

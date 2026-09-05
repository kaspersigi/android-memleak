PYTHON ?= python3
JOBS ?= $(shell nproc)

.PHONY: all release prepare verify test help
all: release
release:
	$(PYTHON) scripts/build-memleak.py --jobs $(JOBS)
prepare:
	$(PYTHON) scripts/build-memleak.py --prepare-only
verify:
	$(PYTHON) scripts/build-memleak.py --verify-only
test:
	$(PYTHON) -m unittest discover -s tests -v
help:
	$(PYTHON) scripts/build-memleak.py --help

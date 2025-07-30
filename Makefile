FSSTRATIFY_VENV = .v

default: tests


.PHONY: init
init: $(FSSTRATIFY_VENV)


$(FSSTRATIFY_VENV):
	@python3 -m venv $@
	@. $(FSSTRATIFY_VENV)/bin/activate && \
	pip install -U pip poetry && \
	poetry install --with dev,docs,test


.PHONY: pylint
pylint:
	@pylint --rcfile=pylintrc fsstratify


.PHONY: tests
tests: unit-tests


.PHONY: unit-tests
unit-tests: $(FSSTRATIFY_VENV)
	@. $(FSSTRATIFY_VENV)/bin/activate && \
	py.test -c pyproject.toml --cov


.PHONY: docs
docs: $(FSSTRATIFY_VENV)
	@. $(FSSTRATIFY_VENV)/bin/activate && \
	$(MAKE) -C docs/ html


.PHONY: serve-docs
serve-docs: docs
	@. $(FSSTRATIFY_VENV)/bin/activate && \
	cd docs && sphinx-serve

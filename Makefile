PY ?= .venv/bin/python
export PYTHONPATH := src

.PHONY: dev install lint test cov privacy audit lock schema image image-multiarch image-previous compose-up compose-down container-test run clean

install:            ## create .venv and install the app with dev tools
	python3 -m venv .venv || python3 -m venv --without-pip .venv
	$(PY) -m pip install -e '.[dev]'

dev:                ## run the app locally with auto-reload (data in ./data)
	HBUDGET_DATA_DIR=./data HBUDGET_DEV=1 $(PY) -m uvicorn hbudget.web.app:create_app --factory --reload --host 127.0.0.1 --port 8080

lint:               ## ruff + mypy
	$(PY) -m ruff check src tests scripts docker
	$(PY) -m mypy

test:               ## all tests (private regression tests skip without BUDGET_PRIVATE_DIR)
	$(PY) -m pytest

cov:                ## tests with coverage; the core module must stay at 90% or more
	$(PY) -m pytest --cov=hbudget --cov-report=term-missing
	$(PY) -m pytest tests/core --cov=hbudget.core --cov-fail-under=90 -q

privacy:            ## personal-data scan (repo + git history)
	$(PY) scripts/privacy_scan.py --repo --history

audit:              ## known-vulnerability check of the dependencies (installed and pinned)
	$(PY) -m pip_audit --skip-editable  # the app itself is not on PyPI, so not --strict here
	$(PY) -m pip_audit --strict --disable-pip -r requirements.lock

schema:             ## copy the schema and the starter rules into the package (tests check the copies match)
	cp schema/bill-system.schema.json examples/starter-rules.yaml src/hbudget/schema/

lock:               ## re-pin the runtime dependencies and the build backend with hashes (needs uv)
	uv pip compile pyproject.toml --universal --python-version 3.13 --generate-hashes -o requirements.lock
	uv pip compile requirements-build.in --universal --python-version 3.13 --generate-hashes --no-header \
	  --no-annotate -o requirements-build.lock

IMAGE ?= hearthfund:latest
PLATFORMS ?= linux/amd64,linux/arm64
PREVIOUS_REF ?=  # the last release tag; empty until the first release (the upgrade test is then skipped)

image:              ## build the Docker image for this machine (loaded into the local image store)
	docker buildx build --load -t $(IMAGE) .

image-multiarch:    ## build for amd64 and arm64 (needs a buildx builder that lists both; no push)
	docker buildx build --platform $(PLATFORMS) -t $(IMAGE) .

image-previous:     ## build hearthfund:previous from PREVIOUS_REF, for the upgrade test
	@if [ -z "$(PREVIOUS_REF)" ]; then echo 'PREVIOUS_REF is empty: no earlier release to build'; exit 1; fi
	rm -rf .prev-build && git worktree add --detach .prev-build $(PREVIOUS_REF)
	cp Dockerfile .dockerignore requirements.lock requirements-build.lock .prev-build/
	mkdir -p .prev-build/docker && cp docker/healthcheck.py .prev-build/docker/
	docker buildx build --load -t hearthfund:previous .prev-build; rc=$$?; git worktree remove --force .prev-build; exit $$rc

compose-up:         ## start with Docker Compose (data in ./data)
	docker compose up -d

compose-down:       ## stop the containers (data stays in ./data)
	docker compose down

container-test: image  ## fresh start, restart, upgrade and hardening tests in throwaway projects
	@# the upgrade test starts from an image built from PREVIOUS_REF, the last release; skipped while it is empty
	if [ -n "$(PREVIOUS_REF)" ]; then $(MAKE) image-previous && python3 scripts/container_test.py --image $(IMAGE) --previous hearthfund:previous; \
	else python3 scripts/container_test.py --image $(IMAGE) --no-upgrade; fi

run: compose-up

clean:
	rm -rf .prev-build .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build dist

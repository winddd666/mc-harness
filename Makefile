PYTHON ?= /home/xzq/projects/mc/.venv/bin/python
PYTEST ?= /home/xzq/projects/mc/.venv/bin/pytest
MC_ENV_FILE ?= /home/xzq/projects/mc/.env.local
MC_STATE_DIR ?= /home/xzq/projects/mc/.state-5ease
MC_RCON_TARGET ?= 5ease
VERIFY_BOT ?= HY_5E_4

.PHONY: test test-fast lint size-check verify-live

test:
	@if $(PYTHON) -c "import xdist" 2>/dev/null; then \
		$(PYTEST) -n auto -m "not live" -q; \
	else \
		$(PYTEST) -m "not live" -q; \
	fi

test-fast:
	$(PYTEST) tests/test_v2_harness.py tests/skills tests/mining -q

size-check:
	$(PYTHON) scripts/check_file_sizes.py

lint: size-check
	@if $(PYTHON) -m ruff --version >/dev/null 2>&1; then \
		$(PYTHON) -m ruff check src tests scripts; \
	else \
		echo "ruff not installed in .venv; skipping ruff check (run 'uv sync --dev' to enable)"; \
	fi
	$(PYTHON) -m compileall -q src scripts

verify-live:
	NO_PROXY="$${NO_PROXY},10.50.249.97" no_proxy="$${no_proxy},10.50.249.97" \
	MC_ENV_FILE=$(MC_ENV_FILE) MC_RCON_TARGET=$(MC_RCON_TARGET) MC_STATE_DIR=$(MC_STATE_DIR) \
	$(PYTHON) -u scripts/verify_historical_bugs_live.py --bot $(VERIFY_BOT)

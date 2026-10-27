# Convenience targets. `make` is optional: each target is one plain python command (see docs/staging-checklist.md).
PYTHON ?= python3
OUT ?= staging-checklist-results.md

.PHONY: staging staging-full staging-previews test

# Pre-launch checklist against production (sampled) + known-outcome backtest. Exit 0 = GREEN LIGHT.
staging:
	$(PYTHON) scripts/staging_checklist.py --out $(OUT)

# Every confirmed venue live + offline pass over every map venue.
staging-full:
	$(PYTHON) scripts/staging_checklist.py --mode full --out $(OUT)

# Production plus the open PR previews listed in scripts/staging_checklist.py.
staging-previews:
	$(PYTHON) scripts/staging_checklist.py --known-previews --out $(OUT)

test:
	$(PYTHON) -m pytest -q

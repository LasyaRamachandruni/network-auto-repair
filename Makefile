PY ?= python3
.PHONY: test lab status watch experiment summary destroy

test:
	$(PY) -m pytest -q

lab:
	$(PY) -m netrepair deploy

status:
	$(PY) -m netrepair status

watch:
	$(PY) -m netrepair watch $(if $(DRY_RUN),--dry-run)

experiment:
	$(PY) -m netrepair experiment --runs 3
	$(PY) -m netrepair.summary

summary:
	$(PY) -m netrepair.summary

destroy:
	$(PY) -m netrepair destroy

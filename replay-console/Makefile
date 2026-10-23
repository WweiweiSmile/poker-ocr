PY ?= ../.venv/bin/python
PORT ?= 8010

.PHONY: install migrate api dev build test check

install:
	cd .. && uv pip install --python .venv/bin/python -r replay-console/requirements.txt
	cd replay_web && pnpm install

migrate:
	$(PY) -c "import sys; sys.path.insert(0,'.'); from replay.config import Settings; from replay.db import Database; print(Database(Settings.from_env()).migrate())"

api:
	$(PY) -m uvicorn replay_api:app --host 127.0.0.1 --port $(PORT)

dev:
	cd replay_web && pnpm dev

build:
	cd replay_web && pnpm build

test:
	CI=true $(PY) -m pytest tests -q

check: test build

# One command to a working board on this machine (LocalNet, no funds, no cloud account).
#
#   make demo       chain -> contract -> D1 -> edge -> one seeded answer
#   make demo-stop  stop the edge (add CHAIN=1 to stop LocalNet too)
#   make e2e        the 15-check edge suite against the running board
#   make seed       stake one more answer
#   make deploy     publish site/ to Cloudflare Pages (needs wrangler login)

SHELL := /bin/bash
PORT ?= 8789
BASE ?= http://127.0.0.1:$(PORT)
PY := uv run --quiet --with py-algorand-sdk python

.PHONY: demo demo-stop seed e2e deploy whitepaper

demo:
	PORT=$(PORT) ./scripts/demo.sh

demo-stop:
	./scripts/demo-stop.sh $(if $(CHAIN),--chain,)

seed:
	$(PY) scripts/seed_localnet.py --base $(BASE) --slot $(or $(SLOT),1) --pay-read

e2e:
	@test -f .demo-env && set -a && . ./.demo-env && set +a; \
	$(PY) scripts/e2e_worker_localnet.py --base $(BASE) --app-id $${APP_ID:-0}

deploy:
	npx --yes wrangler@latest pages deploy site --project-name avouch --branch main --commit-dirty=true

whitepaper:
	uv run --with markdown python scripts/build_whitepaper.py

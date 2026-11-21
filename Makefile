.PHONY: install test test-frontend run frontend eval report migrate openapi up down lint-compose k8s-validate tf-validate workflows-validate release-check backup-drill
install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -r backend/requirements-dev.txt
	cd frontend && npm ci
test:
	cd backend && ../.venv/bin/python -m pytest -q
test-frontend:
	cd frontend && npx tsc --noEmit && npx vitest run && npx vite build
run:
	cd backend && ../.venv/bin/uvicorn app.main:create_app --factory --reload
frontend:
	cd frontend && npm run dev
eval:                      ## measured evaluation (mock components unless you configure real ones); writes experiments/results/
	cd backend && ../.venv/bin/python -m app.evaluation.calibrate ../experiments/results
	cd backend && ../.venv/bin/python -m app.evaluation --out ../experiments/results --abstain-threshold calibrated
	.venv/bin/python experiments/render_report.py
migrate:                   ## needs NEXUS_DATABASE_URL pointing at PostgreSQL
	cd backend && ../.venv/bin/alembic upgrade head
openapi:
	.venv/bin/python infrastructure/scripts/export_openapi.py
up:
	docker compose up --build -d
down:
	docker compose down
lint-compose:
	NEXUS_JWT_SECRET=x docker compose config -q

k8s-validate:   ## static checks of deploy/k8s (no cluster needed)
	backend/.venv/bin/python infrastructure/scripts/validate_k8s.py 2>/dev/null || .venv/bin/python infrastructure/scripts/validate_k8s.py

tf-validate:   ## static Terraform policy checks (no cloud access)
	.venv/bin/python infrastructure/scripts/validate_terraform.py

workflows-validate:   ## static GitHub Actions policy checks
	.venv/bin/python infrastructure/scripts/validate_workflows.py

release-check:   ## version consistency, single alembic head, migration lint, OpenAPI freshness
	.venv/bin/python infrastructure/scripts/release.py check

backup-drill:   ## usage: make backup-drill MANIFEST=/backups/nexus-<ts>.manifest.json
	.venv/bin/python infrastructure/scripts/backup.py drill --manifest $(MANIFEST)

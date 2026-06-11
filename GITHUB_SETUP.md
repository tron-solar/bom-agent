# Getting this into GitHub + Railway

This repo is initialized locally with a clean first commit. You can't push from this sandbox
(no GitHub credentials here), so create the remote and push from your machine — two options below.

## Option A — GitHub CLI (fastest)
```bash
# from the unzipped bom-trigger/ directory
gh repo create tron-bom-trigger --private --source=. --remote=origin --push
```
That creates the private repo, sets the remote, and pushes `main` in one step.

## Option B — manual
```bash
# 1) create an EMPTY repo on github.com (no README/license), copy its URL
# 2) from the unzipped bom-trigger/ directory:
git remote add origin git@github.com:<you>/tron-bom-trigger.git   # or https URL
git branch -M main
git push -u origin main
```

If git isn't initialized yet on your machine (the sandbox commit didn't travel with the zip):
```bash
git init
git add .
git commit -m "BOM trigger service: Coperniq create_bom webhook -> DRAFT + notify"
```

## Connect Railway
1. Railway → **New Project** (or your existing project) → **Deploy from GitHub repo** → pick `tron-bom-trigger`.
2. **Add a Volume**, mount path = `/data/bom-files` (matches `FILE_STORAGE_DIR`).
3. **Variables** tab → set everything from `.env.example` with real values
   (`PUBLIC_BASE_URL` = the Railway-generated domain for this service).
4. Railway auto-detects the Procfile/`railway.toml`. Confirm the build installs `libreoffice-calc`
   (the `railway.toml` aptPkgs line). If your existing services use a shared Dockerfile/nixpacks
   base, mirror that instead.
5. Copy the validated engine files into `engine/` if you didn't commit them
   (`racking_engine.py`, `filter_blank_rows.py`, `orientation_detector.py`, `BOM_TEMPLATE.xlsx`),
   and wire `engine/orchestrator.extract_planset()` to the planset extractor when ready.

## Point Coperniq at it
Coperniq Automation → on `create_bom` work-order ASSIGNED →
`POST https://<railway-domain>/webhooks/coperniq/create-bom`
with your signing header. Match the signing scheme to your other handlers (see
`app/main.py:verify_signature`).

## Sanity check after deploy
```bash
curl https://<railway-domain>/healthz          # {"ok": true, "shadow_mode": true}
# simulate a trigger (only safe in shadow mode):
curl -X POST https://<railway-domain>/webhooks/coperniq/create-bom \
  -H "Content-Type: application/json" \
  -d '{"project_id": 857222, "task_key": "create_bom"}'
```
(With a webhook secret set, you'll need the signature header — see the test file for how it's computed.)

## What's left before it can run unattended
The service is complete; the one boundary still to wire is `engine/orchestrator.extract_planset()`
— it raises `NeedsHumanExtraction` today, which routes to the "generation failed, needs human"
comment. See `AUTONOMY_READINESS_SPEC.md` §A for exactly what extraction must produce and which
confidence gates must fire before flipping off shadow mode.

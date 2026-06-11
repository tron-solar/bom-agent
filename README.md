# Tron Solar — BOM Trigger Service

FastAPI service that listens for the Coperniq **`create_bom`** work-order trigger, generates the
BOM Excel + confidence report, hosts the file on a public route, attaches it to the Coperniq project
as a **DRAFT**, and notifies the `create_bom` assignee via an @mention comment.

Designed to drop alongside your existing Railway Coperniq-trigger handlers. It owns one new webhook
route and reuses the same Coperniq API + Railway-volume patterns you already run.

---

## Flow (shadow mode)
```
Coperniq create_bom WO opens
   └─▶ Coperniq Automation/webhook → POST /webhooks/coperniq/create-bom
          1. verify signature/secret, parse project_id
          2. get_project (read mount/battery/utility/upgrade + create_bom assignee)
          3. download the stamped planset PDF from project files
          4. run the BOM engine → BOM_<First>_<Last>.xlsx + <Last>_confidence_report.json
          5. write both to the Railway volume (served at /files/<token>/<name>)
          6. Coperniq create_project_file(url=<public file url>, name="DRAFT — BOM …")
          7. Coperniq create_project_comment(body="[Assignee|~id:NN] DRAFT ready … flags: N/M")
   └─▶ human reviews flags, approves, completes the work order
```
The service **never completes the work order** — approval is the human's action. See
`AUTONOMY_READINESS_SPEC.md` for what must be true before this can ship unattended.

---

## Repo layout
```
bom-trigger/
├── app/
│   ├── main.py            # FastAPI app: webhook route + static file route + health
│   ├── coperniq.py        # thin Coperniq REST client (read, attach-by-URL, comment, WO update)
│   ├── pipeline.py        # orchestration: project → planset → engine → host → attach → notify
│   ├── hosting.py         # write to Railway volume, mint a public /files URL
│   ├── config.py          # env-var config
│   └── models.py          # webhook payload + internal dataclasses
├── engine/                # the BOM engine (drop your validated modules here)
│   ├── racking_engine.py  # <-- copy from the BOM project (canonical)
│   ├── filter_blank_rows.py
│   ├── orientation_detector.py
│   ├── planset_extractor.py   # <-- the extraction layer (still being validated)
│   └── BOM_TEMPLATE.xlsx      # canonical template
├── requirements.txt
├── Procfile               # Railway start command
├── railway.toml           # Railway config (volume mount, healthcheck)
├── .env.example
└── .gitignore
```

The `engine/` folder is a placeholder boundary: copy in the validated engine files from the BOM
project. `pipeline.py` calls them; it does not reimplement them.

---

## Environment variables (set in Railway)
| Var | Purpose |
|-----|---------|
| `COPERNIQ_API_BASE` | Coperniq REST base, e.g. `https://api.coperniq.io` (match your other handlers) |
| `COPERNIQ_API_KEY` | Coperniq API key/token |
| `COPERNIQ_WEBHOOK_SECRET` | shared secret to verify inbound webhooks |
| `ANTHROPIC_API_KEY` | for the planset extractor (Claude Vision) |
| `PUBLIC_BASE_URL` | this service's public Railway URL, e.g. `https://bom-trigger.up.railway.app` |
| `FILE_STORAGE_DIR` | Railway volume mount path, e.g. `/data/bom-files` |
| `FILE_URL_TTL_HOURS` | how long a hosted file URL stays valid (default 168 = 7 days) |
| `CREATE_BOM_TASK_KEY` | the Coperniq custom-field key for the trigger, default `create_bom` |
| `SHADOW_MODE` | `true` = always attach DRAFT + flag for review (default); `false` reserved for future |
| `LIBREOFFICE_BIN` | path to soffice/libreoffice for recalc (default `libreoffice`) |

Copy `.env.example` → `.env` for local dev. In Railway set them in the service Variables tab.

---

## Coperniq webhook setup
Point a Coperniq Automation at:
```
POST https://<PUBLIC_BASE_URL>/webhooks/coperniq/create-bom
Header: X-Coperniq-Signature: <hmac of body with COPERNIQ_WEBHOOK_SECRET>   (or your existing scheme)
```
Trigger condition: the `create_bom` task/work-order transitions to ASSIGNED (or your equivalent).
**Match this to however your other Coperniq webhooks authenticate** — `coperniq.py` /
`verify_signature()` has a single place to align the scheme.

---

## Deploy
1. Push this repo to GitHub (see `GITHUB_SETUP.md`).
2. In Railway: New Service → Deploy from GitHub repo → select it.
3. Add a Volume, mount at `FILE_STORAGE_DIR` (e.g. `/data/bom-files`).
4. Set the env vars above.
5. Ensure the build installs LibreOffice (see `nixpacks`/`railway.toml` note) for xlsx recalc.
6. Copy the validated engine files into `engine/` (they're gitignored as binaries-by-policy only if
   you choose; otherwise commit them).

---

## Local run
```
pip install -r requirements.txt
cp .env.example .env   # fill in
uvicorn app.main:app --reload --port 8000
# simulate a webhook:
curl -X POST localhost:8000/webhooks/coperniq/create-bom \
  -H "Content-Type: application/json" \
  -d '{"project_id": 857222, "event": "task.assigned", "task_key": "create_bom"}'
```

---

## Safety properties (by design)
- **Idempotent:** a processed `(project_id, task)` is recorded; a duplicate webhook is a no-op.
- **Planset confirmed, not guessed:** the planset is selected by STRICT convention match
  (`<First> <Last> REV<L>.pdf`, highest revision wins). If exactly one match isn't found, the
  pipeline RAISES and notifies a human — it never falls back to an arbitrary PDF. Optional
  second-stage PV-1 content check verifies the file belongs to this project. See
  `app/planset_confirm.py`.
- **Fail loud:** extraction/recalc failure posts a "generation failed, needs human" comment instead
  of attaching a partial BOM.
- **DRAFT only:** filename prefix `DRAFT — `; the work order is never auto-completed.
- **Shadow mode default:** every run surfaces its confidence flags for human review.

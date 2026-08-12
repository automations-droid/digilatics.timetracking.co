# Digilatics Time Intelligence — secure, role-based

Dashboard behind login with three roles. **Postgres** is the system of record
(not Google Sheets). Python jobs sync ClickUp time entries and Google Calendar
meetings; the API returns **only** the rows a user is allowed to see.

| Role | Sees |
|------|------|
| **admin** | everything |
| **lead** | only their team(s) (+ own rows) |
| **employee** | only their own hours ("My Time") |

```
├── main.py                 # FastAPI (auth + scoped /api/data)
├── db.py                   # Postgres models / upserts
├── matching.py             # client matcher (ported from n8n)
├── sheets_refs.py          # master/employee/launchpad lists (gviz, phase 1)
├── ingest/
│   ├── clickup.py          # ClickUp → Postgres
│   ├── meet.py             # Calendar → Postgres
│   ├── import_sheet.py     # one-shot Sheet1 history import
│   ├── scheduler.py        # APScheduler every N minutes
│   └── cutover_notes.txt   # turn off n8n Sheet appends
├── static/app.html
├── docker-compose.yml      # app + Postgres
├── .env.example
└── users.example.json
```

## Configure

```bash
cp .env.example .env
cp users.example.json users.json
# drop service-account.json (Calendar domain-wide SA) in the project root
python -c "import secrets;print(secrets.token_urlsafe(48))"   # -> SESSION_SECRET
```

Required `.env`: `DATABASE_URL`, `CLICKUP_API_TOKEN`, `GOOGLE_SERVICE_ACCOUNT_FILE`,
`GOOGLE_ADMIN_EMAIL` (Workspace admin for directory + calendar delegation).

## Run

Docker (recommended):
```bash
docker compose up --build     # http://localhost:8000
docker compose exec time python -m ingest.import_sheet   # once
```

Local (Postgres already running):
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
# DEV_LOGIN=true → http://localhost:8000/login?as=areebah@digilatics.com
```

Manual syncs:
```bash
python -m ingest.clickup
python -m ingest.meet
```

Admin API: `POST /api/sync/clickup|meet|sheet_import`, `GET /api/sync/status`.

## Cutover from n8n

See `ingest/cutover_notes.txt`. Disable the Sheet **append** nodes once Postgres
parity looks good. Keep master client + employee sheets readable until phase 2.

## Notes

- Time entries use unique `entry_id` upserts (idempotent re-runs).
- Master client / employee / Launchpad lists still load via public gviz URLs in
  phase 1; move those into Postgres later if desired.
- Active timers endpoint currently returns `[]` (old Sheet tab removed).

# Live demo: Sewa Setu assistants (8 minutes)

## One-time setup (your laptop, Docker Desktop running)
`.env` next to docker-compose.yml (never commit it):
```
ADMIN_PASSWORD=demo-officer-pass
SCHEME_DEADLINE=2027-12-31 23:59
GEMINI_API_KEY=<your key>
GEMINI_MODEL=gemini-2.5-flash
```
`SCHEME_DEADLINE` is a demo-only override. The department's real deadline (31 Oct 2026) stays in app.ini; with the
override set, every portal page shows a demo banner saying so. Remove the line to see the honest "window closed" behaviour.

Start clean (this wipes demo data; the 100k-row seed reloads, 3-5 minutes):
```
docker compose down -v
docker compose up -d --build
```
Open http://localhost:8000 (portal), http://localhost:8000/admin (admin / your ADMIN_PASSWORD),
http://localhost:8000/__gateway/ (simulated SMS inbox), http://localhost:8000/eval-dashboard.
Note: on first start the nightly job auto-approves the 700 old seeded pending cases, so the officer queue starts empty.
The citizen demo creates the case the officer then decides.

Run the scripted evals once before the demo so the dashboard has a fresh run (about 2 minutes):
```
docker compose --profile evals run --rm evals
```

## The flow
**0:00 Frame it (30 s).** "A woman of 70 and her grandson. The portal is hard to use; an AI assistant could help, but only
if it can act safely. I built two MCP servers over the existing portal: one for citizens, one for the block officer.
The portal stays the system of record."

**0:30 Citizen (3 min).** Terminal 1:
```
docker compose --profile evals run --rm evals python demo/chat.py citizen
```
Type, as the grandson (use `/upload` when the link appears, or `--auto-upload` to skip that step):
1. `Hi, my grandmother is 70 and lives in Sonari. Can you help her apply for the pension?`
2. `Her name is Kamla Devi, born 4 July 1956, village Lakhipur, Sonari block.`
3. `Bank account 123456789012. I don't know what IFSC is.` then `The passbook says SBIN0003077. She is a widow.`
4. `/upload`, then `Yes, I confirm the declaration.`
Point at: the signed-in phone (not a name), tools being called, the raw portal answer, the application number.

**3:30 Prove it is real (1 min).** Portal /admin, search the application: it is there, with an audit entry marked `channel=agent`.
"Nothing the model says counts; this is the record."

**4:30 Officer (2 min).** Terminal 2:
```
docker compose --profile evals run --rm evals python demo/chat.py officer
```
`What is waiting for me?` -> `Show me Kamla Devi's case.` -> `Reject it.` (the tool refuses: a reason is required)
-> `Reject it: the certificate is not legible.` Show the audit trail in /admin (recorded as `officer:admin`).
Then return to the citizen chat (or a new one with the same --mobile) and ask `What happened to my application?`:
it reports the rejection and the officer's reason.

**6:30 Evals (1.5 min).** /eval-dashboard: the personas, database checks per scenario, the history of runs, and the
"what building this found" list. Say: "I broke the duplicate rule on purpose and the suite failed, so I know it can fail."

## Questions to be ready for
- Why can't a tool take a phone number? Identity comes only from the OAuth token, so a chat cannot impersonate anyone.
- How do web and assistant stay consistent? Both call service.submit_application / decide_application.
- What if a submit times out? Retrying returns the same application number (idempotent; advisory lock per phone).
- What would you do next? Per-officer accounts and block scoping, a real document-capture flow, the same evals on more models.
- Limits you state yourself: one shared officer login, public simulated SMS inbox, model-driven tests used a free-tier Gemini model.

## If something breaks live
Show the dashboard recording and the Loom/screen recording you made as a backup. Rehearse once, and record it.

# Deploying the web layer

What runs in production is the FastAPI app in `web.py` (customer intake at `/`,
admin audit view at `/admin`, liveness at `/health`), served by uvicorn.
`railway.toml` in the repo root is the config-as-code for the primary target
(Railway); a Render fallback is at the bottom. CI (`.github/workflows/ci.yml`)
must be green before any deploy: it runs the offline selftest, the eval-suite
schema check, and a web-layer import smoke on every push.

Two platform facts shape everything below:

- **The container filesystem is ephemeral.** The audit store is an append-only
  JSONL file (`audit_log.jsonl`), so without a volume it resets on every
  deploy/restart. Fine for a demo; attach a volume for anything real.
- **Run exactly one replica** (`numReplicas = 1`, already set). The JSONL
  store is per-disk; replicas would each see a different audit trail. The
  Postgres move is the scale path (see ARCHITECTURE.md).

## Railway (primary)

`railway.toml` already pins the builder to **Nixpacks** — do not remove that:
the repo's `Dockerfile` is the offline-selftest image (no fastapi/uvicorn) and
Railway would otherwise auto-select it and crash-loop. Nixpacks reads
`runtime.txt` (Python 3.12, matching CI) and installs `requirements.txt`.

> Observed on the first live deploy (2026-07-08): on **CLI uploads**
> (`railway up`), Railway's build detection preferred the Dockerfile it found
> in the upload despite the `railway.toml` pin — producing exactly the
> selftest-image crash-loop described above (the container runs the offline
> selftest, exits, and the `/health` check never answers). `.railwayignore`
> now excludes `Dockerfile`/`.dockerignore` from uploads, which makes the
> Nixpacks pin unconditional. Keep it.

1. Create the project (dashboard: *New Project → Deploy from GitHub repo*, or CLI):

   ```bash
   npm i -g @railway/cli        # or: brew install railway
   railway login
   railway init                 # from the repo root; creates/links a project
   ```

2. Set the service variables (dashboard *Variables* tab, or CLI):

   ```bash
   railway variables --set ANTHROPIC_API_KEY=<your key>     # never commit this
   railway variables --set ADMIN_TOKEN=<long random string> # see Security below
   ```

3. Deploy and watch:

   ```bash
   railway up
   ```

4. Optional — persist the audit trail: add a **Volume** to the service
   (e.g. mount at `/data`) and set:

   ```bash
   railway variables --set INTAKE_AUDIT_LOG=/data/audit_log.jsonl
   ```

5. Verify (see the checklist at the bottom).

Railway health-checks `GET /health` (configured in `railway.toml`) and only
routes traffic once it returns 200. `/health` reports key *presence* only —
it never echoes any part of a secret, even masked.

## Environment variables

| Variable | Required | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | **yes** | The only hard-required var. `web.py` fails fast at startup without it (the app never takes traffic misconfigured). |
| `ADMIN_TOKEN` | **yes, before public exposure** | Deploy contract: the bearer token gating `/admin` and `/admin/{id}`. **Verify it is enforced on your build before exposing the service** — see Security posture. |
| `PORT` | injected by platform | Honored via the start command (`uvicorn ... --port $PORT`). Do not set it manually on Railway/Render. `web.py`'s own `INTAKE_WEB_PORT` (default 8080) is for local runs only. |
| `INTAKE_BIND_HOST` | no (not used here) | Only read by the stdlib dev server (`agent.py --serve`), not by `web.py`/uvicorn — the start command already binds `0.0.0.0`. Set it only if you containerize the dev server instead. |
| `INTAKE_AUDIT_LOG` | recommended | Absolute path for the JSONL audit store. Point it at a mounted volume to survive redeploys; defaults to `audit_log.jsonl` beside `web.py` (ephemeral). |
| `INTAKE_CONFIDENCE_THRESHOLD` | no (default 0.70) | Below this, a read gets the escalation-model reread, then human routing. |
| `INTAKE_MAX_RETRIES` | no (default 2, max 8) | Transient-provider-error retries per model call (backoff + jitter). |
| `INTAKE_PRIMARY_MODEL` / `INTAKE_FALLBACK_MODEL` / `INTAKE_ESCALATION_MODEL` | no | Override the Haiku → Sonnet → Opus chain. |

No other configuration is read. Secrets live only in platform variables —
never in the repo, the image, or `railway.toml`.

## Security posture — read before exposing a public URL

- **`/admin` auth (`ADMIN_TOKEN`)** and a **per-IP rate limit on the intake
  form** are the deploy contract for public exposure: the admin view shows
  every customer's raw request text, and each intake submission spends real
  model calls, so an unthrottled `POST /` is an open wallet.
- Both are enforced in `web.py`: `/admin` returns 403 without the token, and
  `POST /` rate-limits per client IP. **Behind a platform edge the limiter
  only works in proxy-header mode** — the edge presents a different socket
  peer per request, so keying on the raw peer means no bucket ever repeats
  (observed live: thirty straight 400s on the checklist probe). That is why
  `railway.toml`'s start command runs uvicorn with `--proxy-headers
  --forwarded-allow-ips '*'`, restoring the real client IP from
  `X-Forwarded-For`; `'*'` is safe on Railway because the edge fronts all
  traffic. Until both verify on *your* build (checklist below), treat the
  deployment as a private demo: keep the URL unshared and don't put customer
  data through it.
- The per-IP rate limit is in-process and per-instance — another reason
  `numReplicas = 1` matters. Platform edge rate-limiting is a fine belt-and-
  suspenders addition if you have it.
- TLS is terminated by the platform on both Railway and Render; nothing to
  configure in-app. API docs routes (`/docs`, `/redoc`) are already disabled.

## Render (fallback)

Render has no config-as-code in this repo; set it up in the dashboard:

1. *New → Web Service*, connect the repo.
2. **Runtime**: Python. Set env var `PYTHON_VERSION=3.12` (Render ignores
   `runtime.txt`).
3. **Build command**: `pip install -r requirements.txt`
4. **Start command**: `uvicorn web:app --host 0.0.0.0 --port $PORT`
5. **Health check path**: `/health`
6. Environment: same table as above (`ANTHROPIC_API_KEY`, `ADMIN_TOKEN`, and
   optionally `INTAKE_AUDIT_LOG` pointed at a **Render Disk** mount for
   persistence; a Disk also forces single-instance, which this app wants).
7. Note: free-tier services spin down when idle; the first request after idle
   is slow and the (disk-less) audit log will have reset.

## Post-deploy verification checklist

```bash
BASE=https://<your-service-url>

curl -fsS "$BASE/health"                  # 200; anthropic_api_key: "set"
curl -fsS "$BASE/" | grep -qi intake      # customer form renders
curl -fsS -o /dev/null -w '%{http_code}\n' "$BASE/admin"
                                          # contract: 401/403 without ADMIN_TOKEN
                                          # (if this returns 200, admin auth has
                                          #  not landed — see Security posture)
# rate limit: hammer the intake form with a deliberately-invalid payload
# ("x" fails validation before any model call, so this spends nothing and
# writes nothing). Expect 400s flipping to 429 once the per-IP cap trips;
# thirty straight 400s means the rate limit has not landed.
for i in $(seq 1 30); do curl -s -o /dev/null -w '%{http_code} ' \
  -X POST "$BASE/" --data-urlencode "text=x"; done; echo
```

Then submit one real request through the browser and confirm it appears in
`/admin` with its full audit trail (validation → hazard screen → model
attempts → confidence gate → routing).

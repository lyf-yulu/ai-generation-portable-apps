# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
# Start everything (Portal + all sub-apps) on port 9090
./Start\ All.command

# Or manually:
cd portal && python3 app.py
```

Portal binds `0.0.0.0:9090` and auto-launches sub-apps on their fixed ports. Individual sub-apps can also run standalone:

```bash
cd seedance && python3 app.py    # port 8787
cd nano-banana && python3 app.py # port 8797
cd dreamina && python3 app.py    # port 8888
```

No third-party Python packages. Pure stdlib (`http.server`, `threading`, `concurrent.futures`, `subprocess`).

## Architecture

```
portal/           → Unified SPA + reverse proxy (port 9090)
├── app.py        → ThreadingHTTPServer: serves static/, proxies /seedance/*, /nano-banana/*, /dreamina/* to sub-apps, tracks usage stats (by_ip), polls job completion
├── static/
│   ├── index.html  → All 4 tabs (Seedance, Nano Banana, Dreamina, 统计)
│   ├── app.js      → Single IIFE: tab switching, form submission, provider binding, stats rendering
│   └── styles.css

seedance/         → Video generation (Seedance 2.0 via T8Star or Volcengine Ark)
├── app.py        → Full app: HTTP handler, job runner (ThreadPoolExecutor), file upload/download, archive system
├── providers.json → Provider configs (base_url, models, defaults per provider)
└── static/       → Standalone UI (used when running without Portal)

nano-banana/      → Image generation (T8Star OpenAI-style or Gemini)
├── app.py        → Same pattern as seedance
├── providers.json
└── static/

dreamina/         → Image/video via Dreamina CLI wrapper
├── app.py        → Wraps `dreamina` CLI tool, manages login/env, polls submit_id for results
├── config.json   → Runtime config (port, max_concurrent, poll intervals)
└── static/
```

## Key Patterns

**Sub-app structure**: Each sub-app is a single `app.py` with:
- `FALLBACK_PROVIDERS` dict (seedance/nano-banana) or `DEFAULT_CONFIG` (dreamina)
- `VALUE_FIELDS` set defining which form fields are extracted
- `run_job()` → spawns `run_one()` per concurrency slot via ThreadPoolExecutor
- `JOBS` dict (in-memory) holding all job state; not persisted across restarts
- `Handler` class extending `SimpleHTTPRequestHandler` with REST endpoints
- `/api/config` returns providers, models, key hint
- `/api/jobs` POST creates jobs, GET returns status
- Archives stored as `.seedance`/`.nanobanana`/`.dreamina` zip files in `archives/`

**Portal proxy**: `_proxy()` reads full response body to extract `job_id` from job-creation responses, then registers the job for usage tracking. All other requests are pass-through.

**Provider system** (seedance, nano-banana): `providers.json` defines available providers with `base_url`, `models[]`, `defaults{}`. Frontend `bindProviderSwitch()` rebuilds model dropdown and updates URL on provider change.

**Output naming**: When `output_name` is set, files are named `{name}-{index}.ext` for multi-concurrency or `{name}.ext` for single runs. Empty means timestamp-based auto-naming.

**Environment detection**: Portal sets `CORS=1` env var on sub-apps. Sub-apps check this to skip auto-opening browser and to add CORS headers.

## Important Constraints

- **Never overwrite git history** — always create new commits, never amend/force-push
- **No pip dependencies** — everything uses Python stdlib only
- **Jobs are in-memory** — restarting kills running tasks; coordinate with users before restart
- **Frontend changes are instant** — Portal serves with `Cache-Control: no-cache`, clients get new version on refresh without restart
- **Backend changes require restart** — which terminates all sub-app processes and running jobs
- **Sub-app ports are fixed** — seedance:8787, nano-banana:8797, dreamina:8888, portal:9090

## File Conventions

- `state/` — runtime JSON (usage, presets, activity logs); gitignored
- `outputs/` — generated files; gitignored
- `archives/` — user-saved presets as zip; gitignored, may contain API keys
- `logs/` — startup/debug logs; gitignored
- `providers.json` — provider/model configuration; committed
- Each app has exactly one `app.py` (no module splitting)

## 回答语言

用中文回答用户问题。

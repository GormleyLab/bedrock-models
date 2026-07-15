# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local, single-user [Chainlit](https://chainlit.io) chat UI over the AWS Bedrock Converse API — a data-secure alternative to hosted chat apps. Chat history (SQLite) and uploads stay on the local machine; only model API calls leave, to the user's own AWS account.

## Commands

Requires **Python 3.13** (not 3.14 — Chainlit has an open event-loop bug there). Setup and run use the venv binaries directly:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/chainlit create-secret   # paste output into .env's CHAINLIT_AUTH_SECRET
# edit .env: set APP_USERNAME / APP_PASSWORD
.venv/bin/chainlit run app.py      # serves http://localhost:8000
```

Add `-w` for auto-reload on file changes, `--headless` to not open a browser. There is no test suite, linter, or build step.

`.env` requires `CHAINLIT_AUTH_SECRET`, `APP_USERNAME`, `APP_PASSWORD`. AWS creds come from the standard chain (`AWS_PROFILE`, `~/.aws/credentials`); region defaults to `us-east-1`. The IAM principal needs `bedrock:ListFoundationModels`, `bedrock:ListInferenceProfiles`, and `bedrock-runtime:Converse*`, plus per-model access granted once in the console (Bedrock → Model access).

## Architecture

Four modules, no framework beyond Chainlit's lifecycle decorators:

- **`app.py`** — Chainlit entrypoint. Owns auth (`password_auth_callback` against `.env` creds), the data layer, the settings/model picker, and the chat loop (`on_message`). Holds per-session state in `cl.user_session`: `history` (ref-based, see below) and `token_totals`.
- **`bedrock.py`** — all AWS interaction. Model discovery + capability detection (`build_catalog`), the cached catalog (`get_catalog`), prompt-cache decisions (`apply_cache_point`), and the two Converse calls (`stream_converse`, `converse_once`).
- **`attachments.py`** — converts Chainlit upload elements into JSON-serializable **refs** and materializes refs back into Converse content blocks with raw bytes.
- **`local_storage.py`** — `LocalStorageClient`, a `BaseStorageClient` writing blobs under `public/uploads/`. Required: without a storage provider, `SQLAlchemyDataLayer` silently drops element attachments on resumed threads.

### Two representations of a message — refs vs. Converse blocks

This is the central design point. Session `history` stores **refs** — `{"role", "content": [...]}` where content blocks are `{"text": ...}`, `{"image_ref": {"path", "format"}}`, or `{"doc_ref": {"path", "format", "name"}}`. Refs are JSON-serializable (paths, not bytes) so they survive in the session. At call time, `materialize_messages()` reads the files and expands refs into real Converse blocks (raw `bytes`), filtered by the current model's capabilities — attachments a model can't accept become a short text placeholder rather than being silently dropped. Files are copied into `data/uploads/` on upload because Chainlit's session temp dir can be cleaned up mid-conversation.

Note there are **two upload dirs**: `data/uploads/` (model-context bytes, referenced by history) and `public/uploads/` (UI display blobs served over Chainlit's static route). Both are gitignored and per-machine.

### Model catalog and capability detection

`build_catalog()` lists `TEXT`-output foundation models, then resolves each to an `invoke_id` — preferring a cross-region inference profile (`us.*`) from `list_inference_profiles`, falling back to the raw model id if `ON_DEMAND`, else skipping it. Capabilities are detected up front where an API exposes them and heuristically otherwise:
- **image** — from `inputModalities` (reliable).
- **document** — no API exposes this, so it's an allowlist by model-id prefix (`_DOC_SUPPORT_PREFIXES`).
- **prompt caching** — assumed for `anthropic.*` only.

The catalog is keyed by display name (`"Provider — Name [img, doc]"`), memoized process-wide behind a lock, and never invalidated during a run.

### Runtime feature-fallback

Some request features can't be detected before a call fails. `_call_with_retry` catches `ValidationException`, strips the offending feature, retries, and **remembers the model** for the session so later turns skip it:
- `temperature` — some models (e.g. Claude Opus 4.8) reject it as deprecated → `_no_temperature_models`.
- `cachePoint` blocks — models without caching reject them (sometimes with a non-obvious message) → `_no_cache_models`.

This is why capability detection above can afford to be heuristic: the runtime path is the safety net.

### Prompt caching (Claude)

`apply_cache_point` appends a `cachePoint` marker to the conversation prefix once an estimated-token threshold (`CACHE_MIN_TOKENS = 4096`) is crossed, so re-sent history bills at ~10% on later turns. Bedrock rejects a `cachePoint` placed directly after a document/image block, so the marker anchors to the most recent message ending in a text block — and correspondingly `materialize_messages` orders **attachments before text** within each turn.

### Streaming across the boto3 sync boundary

boto3 is synchronous. `stream_converse` consumes the Converse stream in a daemon worker thread and hands events to the event loop via an `asyncio.Queue` + `loop.call_soon_threadsafe`, so streaming never blocks Chainlit. `converse_once` (non-streaming models) just uses `asyncio.to_thread`.

### Persistence and migrations

`schema.sql` is a SQLite adaptation of Chainlit's official `SQLAlchemyDataLayer` schema (UUID/JSONB/array columns → `TEXT`; camelCase column names are quoted because the data layer references them literally). `_init_schema()` runs it on every start (idempotent `CREATE TABLE IF NOT EXISTS`) and applies the `_MIGRATIONS` dict to add columns Chainlit introduced after first release, in place, so existing history survives upgrades. When bumping the Chainlit version and hitting a missing-column error at runtime, add the column to `_MIGRATIONS`.

`on_chat_resume` rebuilds session state that doesn't persist: text-only `history` from stored steps and `token_totals` by regex-parsing the usage footers of past replies. The reply `cl.Message` sets `parent_id = None` to detach from the implicit `on_message` step, which is never persisted and would otherwise orphan the reply on resume.

## Gotchas

- The `/public/uploads` route is **unauthenticated** — fine for localhost, do not expose the port to a network.
- Full context (including image/document bytes) is re-sent every turn; the Converse API is stateless. Prompt caching offsets this on Claude; the cache expires after ~5 min idle.
- Accepted upload types and 5 MB limit are configured in **both** `.chainlit/config.toml` (`spontaneous_file_upload`) and `attachments.py` (per-Converse-API limits); keep them consistent when changing.

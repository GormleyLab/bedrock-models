# bedrock-models

A local, single-user [Chainlit](https://chainlit.io) chat UI for AWS Bedrock
models — a data-secure alternative to ChatGPT/Claude web apps. Everything
stays on your machine (chat history in a local SQLite database, uploads on
local disk) except the model API calls to your own AWS account.

## Features

- **Dynamic model discovery** — every text model your account can invoke in
  your region appears in the picker, including models that require
  cross-region inference profiles (newer Claude, Nova, Llama, etc.).
- **Streaming responses** via the Bedrock Converse API, with per-response
  token usage and a running session total.
- **Prompt caching** on Claude models: once a conversation exceeds a few
  thousand tokens, the re-sent history is cached by Bedrock and billed at
  ~10% on subsequent turns (shown as "cached" in the usage footer).
- **Image and document uploads** (PNG/JPEG/GIF/WebP; PDF/Word/Excel/CSV/
  TXT/MD/HTML), gated per-model: the picker tags models with `[img]` / `[doc]`
  and the app warns instead of erroring when a model can't accept a file.
- **Persistent chat history** — past conversations in the sidebar, resumable
  with full multimodal context.
- Adjustable temperature, max tokens, and system prompt per conversation.

## Setup

Prereqs: Python 3.13 (not 3.14 — Chainlit has an open event-loop bug there),
AWS CLI configured with credentials that can call Bedrock
(`bedrock:ListFoundationModels`, `bedrock:ListInferenceProfiles`,
`bedrock-runtime:Converse*`), and model access granted in the AWS console
(Bedrock → Model access).

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\chainlit create-secret   # paste output into .env
# edit .env: set APP_USERNAME / APP_PASSWORD (your local login)
```

## Run

```powershell
.\.venv\Scripts\chainlit run app.py
```

Open http://localhost:8000, log in with the credentials from `.env`, pick a
model in the settings panel (⚙️), and chat.

## Notes

- Chat history lives in `data/chainlit.db`; uploaded files in `data/uploads/`
  (model context) and `public/uploads/` (UI display). All are gitignored.
- Full conversation context — including images/documents — is re-sent to the
  model on every turn (the Converse API is stateless). On Claude models,
  prompt caching offsets most of that cost; the cache expires after ~5
  minutes of inactivity, so the first turn after a long pause pays a small
  (1.25x) cache-write premium again.
- `AccessDeniedException` on a model means access hasn't been granted yet:
  AWS console → Bedrock → Model access.
- The `/public/uploads` route is unauthenticated; that's acceptable for a
  localhost-only app but don't expose the port to a network.

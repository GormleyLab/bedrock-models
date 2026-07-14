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

Prereqs on every machine:

- **Python 3.13** (not 3.14 — Chainlit has an open event-loop bug there).
  - Windows: [python.org](https://www.python.org/downloads/) installer
  - macOS: `brew install python@3.13`
  - Ubuntu / WSL: `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.13 python3.13-venv`
- **AWS credentials** that can call Bedrock (`bedrock:ListFoundationModels`,
  `bedrock:ListInferenceProfiles`, `bedrock-runtime:Converse*`): run
  `aws configure` (or copy `~/.aws/credentials` from another machine), and
  grant model access once per account in the AWS console (Bedrock → Model
  access).

### Windows (PowerShell)

```powershell
git clone https://github.com/GormleyLab/bedrock-models.git && cd bedrock-models
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\chainlit create-secret   # paste output into .env
# edit .env: set APP_USERNAME / APP_PASSWORD (your local login)
```

### macOS / Linux / WSL (bash or zsh)

```bash
git clone https://github.com/GormleyLab/bedrock-models.git && cd bedrock-models
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/chainlit create-secret   # paste output into .env
# edit .env: set APP_USERNAME / APP_PASSWORD (your local login)
```

## Run

Windows:

```powershell
.\.venv\Scripts\chainlit run app.py
```

macOS / Linux / WSL:

```bash
.venv/bin/chainlit run app.py
```

Open http://localhost:8000, log in with the credentials from `.env`, pick a
model in the settings panel (⚙️), and chat. Under WSL2, the server is
reachable from your Windows browser at the same http://localhost:8000 URL.

## Notes

- Chat history lives in `data/chainlit.db`; uploaded files in `data/uploads/`
  (model context) and `public/uploads/` (UI display). All are gitignored —
  each machine keeps its own history; conversations don't sync between
  computers.
- `.env` is also gitignored, so create it (and a fresh `chainlit
  create-secret`) on each machine. The AWS region defaults to `us-east-1`;
  override with `AWS_DEFAULT_REGION` in `.env` if needed.
- Full conversation context — including images/documents — is re-sent to the
  model on every turn (the Converse API is stateless). On Claude models,
  prompt caching offsets most of that cost; the cache expires after ~5
  minutes of inactivity, so the first turn after a long pause pays a small
  (1.25x) cache-write premium again.
- `AccessDeniedException` on a model means access hasn't been granted yet:
  AWS console → Bedrock → Model access.
- The `/public/uploads` route is unauthenticated; that's acceptable for a
  localhost-only app but don't expose the port to a network.

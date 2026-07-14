"""Chainlit entrypoint: auth, persistence, model picker, chat loop."""

import logging
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import chainlit as cl
from botocore.exceptions import ClientError
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.input_widget import Select, Slider, TextInput

import bedrock
from attachments import materialize_messages, process_elements
from local_storage import LocalStorageClient

logger = logging.getLogger(__name__)

DB_PATH = Path("data/chainlit.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Matches the usage footer appended to assistant replies (any variant)
_USAGE_FOOTER_RE = re.compile(r"\n\n\*tokens:[^\n]*\*\s*$")
# Extracts per-turn counts from a footer, for rebuilding session totals
_USAGE_NUMBERS_RE = re.compile(
    r"\*tokens: ([\d,]+) in(?: \+ ([\d,]+) cached)? / ([\d,]+) out"
)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

# Columns added to schema.sql after the first release; applied to existing
# databases in place so chat history survives upgrades
_MIGRATIONS = {
    "steps": {"autoCollapse": "BOOLEAN"},
    "elements": {"autoPlay": "BOOLEAN", "playerConfig": "TEXT"},
}


def _init_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        for table, columns in _MIGRATIONS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns.items():
                if column not in existing:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN "{column}" {decl}')


@cl.data_layer
def get_data_layer():
    _init_schema()
    return SQLAlchemyDataLayer(
        conninfo=f"sqlite+aiosqlite:///{DB_PATH.as_posix()}",
        storage_provider=LocalStorageClient(base_dir="public/uploads"),
    )


# --------------------------------------------------------------------------
# Auth (required by Chainlit for persistent history)
# --------------------------------------------------------------------------

@cl.password_auth_callback
def auth_callback(username: str, password: str) -> Optional[cl.User]:
    expected_user = os.environ.get("APP_USERNAME", "")
    expected_pass = os.environ.get("APP_PASSWORD", "")
    if not expected_user or not expected_pass:
        logger.error("APP_USERNAME / APP_PASSWORD not set in .env")
        return None
    if secrets.compare_digest(username, expected_user) and secrets.compare_digest(
        password, expected_pass
    ):
        return cl.User(identifier=username, metadata={"role": "admin"})
    return None


# --------------------------------------------------------------------------
# Settings / model picker
# --------------------------------------------------------------------------

_DEFAULT_MODEL_HINTS = ("claude opus 4.8", "claude sonnet", "claude", "nova pro")


def _default_model_index(names: List[str]) -> int:
    for hint in _DEFAULT_MODEL_HINTS:
        for i, name in enumerate(names):
            if hint in name.lower():
                return i
    return 0


async def _send_settings(selected: Optional[str] = None) -> Dict[str, Any]:
    catalog = bedrock.get_catalog()
    names = list(catalog.keys())
    if not names:
        raise RuntimeError("No invokable Bedrock models found for this account.")
    if selected in names:
        initial = names.index(selected)
    else:
        initial = _default_model_index(names)
    settings = await cl.ChatSettings(
        [
            Select(
                id="model",
                label="Model",
                values=names,
                initial_index=initial,
                description="[img] = accepts images, [doc] = accepts documents",
            ),
            Slider(id="temperature", label="Temperature", initial=0.5, min=0, max=1, step=0.1),
            Slider(id="max_tokens", label="Max response tokens", initial=4096, min=256, max=8192, step=256),
            TextInput(id="system_prompt", label="System prompt (optional)", initial=""),
        ]
    ).send()
    return settings


def _current_model() -> "bedrock.ModelInfo":
    catalog = bedrock.get_catalog()
    settings = cl.user_session.get("chat_settings") or {}
    name = settings.get("model")
    if name in catalog:
        return catalog[name]
    names = list(catalog.keys())
    return catalog[names[_default_model_index(names)]]


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("history", [])
    cl.user_session.set("token_totals", {"in": 0, "out": 0})
    try:
        await _send_settings()
    except ClientError as e:
        await cl.Message(
            content=f"⚠️ Could not reach AWS Bedrock: `{e.response['Error']['Code']}`. "
            "Check your credentials with `aws sts get-caller-identity` and restart."
        ).send()
    except Exception as e:
        await cl.Message(content=f"⚠️ Failed to load model list: {e}").send()


@cl.on_settings_update
async def on_settings_update(settings: Dict[str, Any]):
    model = _current_model()
    history = cl.user_session.get("history") or []
    has_images = any("image_ref" in b for t in history for b in t["content"])
    has_docs = any("doc_ref" in b for t in history for b in t["content"])
    warnings = []
    if has_images and not model.image_input:
        warnings.append("images")
    if has_docs and not model.document_input:
        warnings.append("documents")
    if warnings:
        await cl.Message(
            content=f"ℹ️ **{model.display_name}** doesn't accept {' or '.join(warnings)} — "
            "attachments already in this conversation will be omitted from its context."
        ).send()


@cl.on_chat_resume
async def on_chat_resume(thread: Dict[str, Any]):
    history = cl.user_session.get("history")
    if not history:
        # Session state didn't survive (e.g. older DB) — rebuild text-only
        # history from persisted steps so the model keeps the context
        history = []
        for step in thread.get("steps", []):
            if step.get("parentId"):
                continue
            text = (step.get("output") or "").strip()
            if not text:
                continue
            if step.get("type") == "user_message":
                history.append({"role": "user", "content": [{"text": text}]})
            elif step.get("type") == "assistant_message":
                text = _USAGE_FOOTER_RE.sub("", text)
                history.append({"role": "assistant", "content": [{"text": text}]})
        cl.user_session.set("history", history)

    if not cl.user_session.get("token_totals"):
        # Rebuild session totals from the usage footers of persisted replies
        totals = {"in": 0, "out": 0}
        for step in thread.get("steps", []):
            if step.get("type") != "assistant_message":
                continue
            m = _USAGE_NUMBERS_RE.search(step.get("output") or "")
            if m:
                totals["in"] += int(m.group(1).replace(",", ""))
                totals["in"] += int((m.group(2) or "0").replace(",", ""))
                totals["out"] += int(m.group(3).replace(",", ""))
        cl.user_session.set("token_totals", totals)

    settings = cl.user_session.get("chat_settings") or {}
    try:
        await _send_settings(selected=settings.get("model"))
    except Exception as e:
        await cl.Message(content=f"⚠️ Failed to load model list: {e}").send()


# --------------------------------------------------------------------------
# Chat loop
# --------------------------------------------------------------------------

_ERROR_HINTS = {
    "AccessDeniedException": (
        "You haven't enabled access to this model yet. Open the AWS console → "
        "Bedrock → **Model access**, request access, then try again."
    ),
    "ThrottlingException": (
        "Bedrock is throttling requests. Wait a moment and try again."
    ),
    "ModelNotReadyException": "The model isn't ready yet — try again shortly.",
    "ServiceUnavailableException": "Bedrock is temporarily unavailable — try again shortly.",
    "ModelTimeoutException": "The model timed out — try again or reduce the input size.",
}


def _friendly_error(e: ClientError, model: "bedrock.ModelInfo") -> str:
    code = e.response.get("Error", {}).get("Code", "UnknownError")
    aws_msg = e.response.get("Error", {}).get("Message", str(e))
    hint = _ERROR_HINTS.get(code, "")
    if code == "ValidationException":
        hint = aws_msg
        if re.search(r"image|document", aws_msg, re.IGNORECASE):
            hint += (
                "\n\nThis model may not support that attachment type — try a model "
                "tagged `[img]` or `[doc]` in the settings panel."
            )
        return f"⚠️ **{code}**: {hint}"
    return f"⚠️ **{code}** on *{model.display_name}*: {hint or aws_msg}"


@cl.on_message
async def on_message(message: cl.Message):
    model = _current_model()
    history: List[Dict] = cl.user_session.get("history") or []
    settings = cl.user_session.get("chat_settings") or {}

    # --- attachments -------------------------------------------------------
    refs, rejected = process_elements(message.elements or [])
    if rejected:
        await cl.Message(
            content="Some attachments were skipped:\n- " + "\n- ".join(rejected)
        ).send()

    unsupported = []
    kept_refs = []
    for ref in refs:
        if "image_ref" in ref and not model.image_input:
            unsupported.append("image")
        elif "doc_ref" in ref and not model.document_input:
            unsupported.append("document")
        else:
            kept_refs.append(ref)
    if unsupported:
        catalog = bedrock.get_catalog()
        capable = [
            n for n, m in catalog.items()
            if ("image" in unsupported and m.image_input)
            or ("document" in unsupported and m.document_input)
        ][:5]
        await cl.Message(
            content=f"ℹ️ **{model.display_name}** doesn't accept "
            f"{' or '.join(sorted(set(unsupported)))} input, so those attachments "
            "were not sent. Models that do: " + ", ".join(f"*{n}*" for n in capable)
        ).send()

    # --- build user turn ---------------------------------------------------
    content: List[Dict] = []
    if message.content and message.content.strip():
        content.append({"text": message.content})
    content.extend(kept_refs)
    if not content:
        await cl.Message(content="Please send a message or a supported attachment.").send()
        return
    history.append({"role": "user", "content": content})

    messages = materialize_messages(
        history,
        include_images=model.image_input,
        include_docs=model.document_input,
    )
    bedrock.apply_cache_point(model, messages)

    max_tokens = int(settings.get("max_tokens") or 4096)
    temperature = float(settings.get("temperature") if settings.get("temperature") is not None else 0.5)
    system_prompt = (settings.get("system_prompt") or "").strip() or None

    # --- call the model ----------------------------------------------------
    msg = cl.Message(content="")
    # Detach from the implicit on_message run step: that step is never
    # persisted, and a dangling parentId hides the reply on thread resume
    msg.parent_id = None
    reply_text = ""
    usage: Optional[Dict[str, int]] = None

    try:
        if model.streaming:
            async for event in bedrock.stream_converse(
                model.invoke_id, messages, system_prompt, max_tokens, temperature
            ):
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]["delta"].get("text", "")
                    if delta:
                        reply_text += delta
                        await msg.stream_token(delta)
                elif "metadata" in event:
                    usage = event["metadata"].get("usage")
        else:
            resp = await bedrock.converse_once(
                model.invoke_id, messages, system_prompt, max_tokens, temperature
            )
            for block in resp["output"]["message"]["content"]:
                if "text" in block:
                    reply_text += block["text"]
            usage = resp.get("usage")
            await msg.stream_token(reply_text)
    except ClientError as e:
        history.pop()  # keep history consistent with what the model has seen
        if reply_text:
            # Mid-stream failure: keep the partial text and stay role-alternating
            history.append({"role": "user", "content": content})
            history.append({"role": "assistant", "content": [{"text": reply_text}]})
        cl.user_session.set("history", history)
        await msg.stream_token(("\n\n" if reply_text else "") + _friendly_error(e, model))
        await msg.send()
        return
    except Exception as e:
        history.pop()
        cl.user_session.set("history", history)
        await msg.stream_token(("\n\n" if reply_text else "") + f"⚠️ Unexpected error: {e}")
        await msg.send()
        return

    history.append({"role": "assistant", "content": [{"text": reply_text}]})
    cl.user_session.set("history", history)

    if usage:
        # Cache reads bill at ~10% of the input rate; cache writes at 1.25x,
        # so writes are folded into the full-price "in" figure
        cache_read = usage.get("cacheReadInputTokens", 0) or 0
        cache_write = usage.get("cacheWriteInputTokens", 0) or 0
        turn_in = (usage.get("inputTokens", 0) or 0) + cache_write
        turn_out = usage.get("outputTokens", 0) or 0

        totals = cl.user_session.get("token_totals") or {"in": 0, "out": 0}
        totals["in"] += turn_in + cache_read
        totals["out"] += turn_out
        cl.user_session.set("token_totals", totals)

        cached_part = f" + {cache_read:,} cached" if cache_read else ""
        await msg.stream_token(
            f"\n\n*tokens: {turn_in:,} in{cached_part} / {turn_out:,} out"
            f" · session: {totals['in']:,} in / {totals['out']:,} out*"
        )
    await msg.send()

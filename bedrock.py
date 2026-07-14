"""AWS Bedrock integration: model discovery, capabilities, and streaming.

Uses the Converse / ConverseStream API, which provides a unified message
schema (text/image/document content blocks) across all model providers.
"""

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

_REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "us-east-1"
)

_bedrock = boto3.client("bedrock", region_name=_REGION)
_runtime = boto3.client(
    "bedrock-runtime",
    region_name=_REGION,
    config=Config(
        retries={"max_attempts": 8, "mode": "adaptive"},
        read_timeout=300,
    ),
)

# No Bedrock API exposes per-model *document* support, so this is a
# heuristic allowlist by model-id prefix. Image support comes from
# inputModalities. A runtime ValidationException is the fallback.
_DOC_SUPPORT_PREFIXES = (
    "anthropic.",
    "amazon.nova-pro",
    "amazon.nova-lite",
    "amazon.nova-premier",
    "amazon.nova-2-pro",
    "amazon.nova-2-lite",
    "amazon.nova-2-premier",
    "meta.llama3-2",
    "meta.llama3-3",
    "meta.llama4",
    "mistral.mistral-large",
    "mistral.pixtral",
    "cohere.command-r",
    "ai21.jamba",
)

# Ids that pass the TEXT-output filter but aren't chat models
_EXCLUDE_ID_FRAGMENTS = ("embed", "canvas", "reel", "image", "rerank", "sonic")


@dataclass
class ModelInfo:
    model_id: str          # foundation model id, e.g. anthropic.claude-sonnet-4-6
    invoke_id: str         # id to pass to converse(), may be a us.* profile id
    display_name: str      # "Anthropic — Claude Sonnet 4.6 [img, doc]"
    provider: str
    streaming: bool
    image_input: bool
    document_input: bool
    prompt_caching: bool = False


_catalog: Optional[Dict[str, ModelInfo]] = None
_catalog_lock = threading.Lock()


def _build_profile_map() -> Dict[str, str]:
    """Map foundation model id -> cross-region inference profile id."""
    profiles: Dict[str, str] = {}
    try:
        token = None
        while True:
            kwargs: Dict[str, Any] = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = _bedrock.list_inference_profiles(**kwargs)
            for prof in resp.get("inferenceProfileSummaries", []):
                if prof.get("status") != "ACTIVE":
                    continue
                prof_id = prof["inferenceProfileId"]
                for m in prof.get("models", []):
                    arn = m.get("modelArn", "")
                    model_id = arn.rsplit("/", 1)[-1] if "/" in arn else ""
                    if not model_id:
                        continue
                    # Prefer us.* profiles (matches us-east-1); keep the first
                    # otherwise as a fallback
                    if prof_id.startswith("us.") or model_id not in profiles:
                        profiles[model_id] = prof_id
            token = resp.get("nextToken")
            if not token:
                break
    except Exception as e:
        logger.warning(
            "Could not list inference profiles (%s); only ON_DEMAND models "
            "will be available. Check IAM permissions for "
            "bedrock:ListInferenceProfiles.", e,
        )
    return profiles


def _display_name(provider: str, name: str, image: bool, doc: bool) -> str:
    caps = [c for c, on in (("img", image), ("doc", doc)) if on]
    suffix = f" [{', '.join(caps)}]" if caps else ""
    return f"{provider} — {name}{suffix}"


def build_catalog() -> Dict[str, ModelInfo]:
    """Discover invokable text-generation models. Keyed by display name."""
    profile_map = _build_profile_map()

    resp = _bedrock.list_foundation_models(byOutputModality="TEXT")
    models: List[ModelInfo] = []
    for summary in resp.get("modelSummaries", []):
        model_id = summary["modelId"]
        if summary.get("modelLifecycle", {}).get("status") != "ACTIVE":
            continue
        if any(frag in model_id.lower() for frag in _EXCLUDE_ID_FRAGMENTS):
            continue
        if "TEXT" not in summary.get("outputModalities", []):
            continue

        inference_types = summary.get("inferenceTypesSupported", [])
        profile_id = profile_map.get(model_id)
        if profile_id:
            invoke_id = profile_id  # prefer cross-region profile when available
        elif "ON_DEMAND" in inference_types:
            invoke_id = model_id
        else:
            continue  # requires provisioned throughput or unavailable profile

        provider = summary.get("providerName", model_id.split(".")[0].title())
        image_input = "IMAGE" in summary.get("inputModalities", [])
        document_input = model_id.startswith(_DOC_SUPPORT_PREFIXES)
        models.append(
            ModelInfo(
                model_id=model_id,
                invoke_id=invoke_id,
                display_name=_display_name(
                    provider, summary.get("modelName", model_id),
                    image_input, document_input,
                ),
                provider=provider,
                streaming=summary.get("responseStreamingSupported", False),
                image_input=image_input,
                document_input=document_input,
                # Claude models support Converse cachePoint blocks; a runtime
                # ValidationException fallback covers any that don't
                prompt_caching=model_id.startswith("anthropic."),
            )
        )

    models.sort(key=lambda m: (m.provider.lower(), m.display_name.lower()))

    catalog: Dict[str, ModelInfo] = {}
    for m in models:
        name = m.display_name
        # Different model versions can share a display name; disambiguate
        if name in catalog:
            name = f"{m.display_name} ({m.model_id})"
            m.display_name = name
        catalog[name] = m
    return catalog


def get_catalog() -> Dict[str, ModelInfo]:
    global _catalog
    with _catalog_lock:
        if _catalog is None:
            _catalog = build_catalog()
        return _catalog


_DONE = object()


# Some models reject request features there's no API to detect upfront —
# e.g. Claude Opus 4.8 rejects `temperature` as deprecated, and older Claude
# models may reject cachePoint blocks. On rejection the call is retried
# without the feature and the model is remembered for the session.
_no_temperature_models: set = set()
_no_cache_models: set = set()

# Estimated-token threshold before cache points are added. Below this, either
# the prefix is under Bedrock's per-model caching minimum (marker silently
# ignored) or the chat is too small for the 1.25x cache-write premium to pay off.
CACHE_MIN_TOKENS = 4096


def _validation_message(e: Exception) -> str:
    from botocore.exceptions import ClientError

    if not isinstance(e, ClientError):
        return ""
    err = e.response.get("Error", {})
    if err.get("Code") != "ValidationException":
        return ""
    return err.get("Message", "")


def _strip_cache_points(messages: List[Dict[str, Any]]) -> None:
    for msg in messages:
        msg["content"] = [b for b in msg["content"] if "cachePoint" not in b]


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Rough token estimate of a Converse request, for the caching threshold."""
    total = 0
    for msg in messages:
        for block in msg.get("content", []):
            if "text" in block:
                total += len(block["text"]) // 4
            elif "image" in block:
                total += len(block["image"]["source"]["bytes"]) // 8
            elif "document" in block:
                total += len(block["document"]["source"]["bytes"]) // 8
    return total


def apply_cache_point(model: ModelInfo, messages: List[Dict[str, Any]]) -> bool:
    """Mark the conversation prefix for prompt caching when worthwhile.

    Appends a cachePoint block to the final message so the whole prefix is
    cached; on the next turn Bedrock reads the previous checkpoint (within
    its lookback window) and only processes the new tokens at full price.
    """
    if not model.prompt_caching or model.invoke_id in _no_cache_models:
        return False
    if not messages or estimate_tokens(messages) < CACHE_MIN_TOKENS:
        return False
    # Bedrock rejects a cachePoint directly after a document/image block, so
    # anchor it to the most recent message that ends with a text block
    for msg in reversed(messages):
        if msg["content"] and "text" in msg["content"][-1]:
            msg["content"].append({"cachePoint": {"type": "default"}})
            return True
    return False


def _build_kwargs(
    invoke_id: str,
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str],
    max_tokens: int,
    temperature: float,
) -> Dict[str, Any]:
    inference_config: Dict[str, Any] = {"maxTokens": max_tokens}
    if invoke_id not in _no_temperature_models:
        inference_config["temperature"] = temperature
    kwargs: Dict[str, Any] = {
        "modelId": invoke_id,
        "messages": messages,
        "inferenceConfig": inference_config,
    }
    if system_prompt:
        kwargs["system"] = [{"text": system_prompt}]
    return kwargs


def _call_with_retry(api_call, kwargs: Dict[str, Any]):
    """Call Converse, stripping unsupported request features on rejection."""
    stripped_cache = False
    for _ in range(3):
        try:
            result = api_call(**kwargs)
            if stripped_cache:
                # The retry without cache points succeeded, so they were the
                # problem — skip caching for this model from now on
                _no_cache_models.add(kwargs["modelId"])
            return result
        except Exception as e:
            message = _validation_message(e).lower()
            invoke_id = kwargs["modelId"]
            has_temp = "temperature" in kwargs["inferenceConfig"]
            has_cache = any(
                "cachePoint" in b for m in kwargs["messages"] for b in m["content"]
            )
            if message and "temperature" in message and has_temp:
                logger.info("%s rejects temperature; retrying without it", invoke_id)
                _no_temperature_models.add(invoke_id)
                kwargs["inferenceConfig"].pop("temperature", None)
            elif message and has_cache:
                # Models without caching support reject cachePoint blocks with
                # messages that don't always mention caching (e.g.
                # "messages.0.content.3.type: Field required"), so retry any
                # validation failure once without the cache points
                logger.info(
                    "%s validation error with cache points present (%s); "
                    "retrying without them", invoke_id, message,
                )
                _strip_cache_points(kwargs["messages"])
                stripped_cache = True
            else:
                raise
    return api_call(**kwargs)


async def stream_converse(
    invoke_id: str,
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.5,
) -> AsyncIterator[Dict[str, Any]]:
    """Yield ConverseStream events without blocking the event loop.

    boto3 is synchronous, so the stream is consumed in a worker thread and
    events are handed to the loop through an asyncio.Queue.
    """
    kwargs = _build_kwargs(invoke_id, messages, system_prompt, max_tokens, temperature)

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def worker() -> None:
        try:
            resp = _call_with_retry(_runtime.converse_stream, kwargs)
            for event in resp["stream"]:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)
        except Exception as e:  # surfaced to the async consumer
            loop.call_soon_threadsafe(queue.put_nowait, e)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        item = await queue.get()
        if item is _DONE:
            break
        if isinstance(item, Exception):
            raise item
        yield item


async def converse_once(
    invoke_id: str,
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.5,
) -> Dict[str, Any]:
    """Non-streaming fallback for models without streaming support."""
    kwargs = _build_kwargs(invoke_id, messages, system_prompt, max_tokens, temperature)
    return await asyncio.to_thread(_call_with_retry, _runtime.converse, kwargs)

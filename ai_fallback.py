"""
ai_fallback.py - AI Provider Fallback Chain for Pantry Planner.

Supports:
  1. Google Gemini (default: gemini-3.8-flash) via google.genai
  2. Groq (model: meta-llama/llama-4-scout-17b-16e-instruct) via OpenAI-compatible REST API
  3. OpenRouter (model: openrouter/free or user specified) via OpenAI-compatible REST API

Failover rule:
  On HTTP 503, 429, or request timeout from active provider, retry the same
  request on the next configured provider. If all configured providers fail,
  return a friendly RuntimeError. Malformed JSON also triggers failover.
"""

import base64
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple, Type
from PIL import Image
from pydantic import BaseModel
import requests

logger = logging.getLogger("pantry_planner.ai_fallback")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "qwen/qwen3.8-27b"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "nvidia/nemotron-3.5-super-vl:free"

GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"

DEFAULT_PROVIDER_MODELS = {
    "gemini": GEMINI_DEFAULT_MODEL,
    "groq": GROQ_DEFAULT_MODEL,
    "openrouter": OPENROUTER_DEFAULT_MODEL,
}

PROVIDER_ORDER = ["gemini", "groq", "openrouter"]

# Groq free-tier TPM 8000 + 2048 completion cap truncates full-week plans beyond ~75 items.
GROQ_GENERATION_MAX_ITEMS = 75


class ProviderKeys(BaseModel):
    gemini_key: Optional[str] = None
    groq_key: Optional[str] = None
    openrouter_key: Optional[str] = None


def extract_provider_keys(
    headers: Any,
    env: Optional[Dict[str, str]] = None,
) -> ProviderKeys:
    """Extract provider keys from HTTP request headers, environment variables, or config."""
    if env is None:
        env = os.environ

    get_header = getattr(headers, "get", lambda k, default="": default)

    cfg_keys: Dict[str, str] = {}
    cfg_file = env.get("PANTRY_CONFIG_FILE", "")
    if not cfg_file and not env.get("PANTRY_TEST_MODE"):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(base_dir, "pantry_desktop_config.json"),
            os.path.join(os.path.dirname(base_dir), "pantry_desktop_config.json"),
            "pantry_desktop_config.json",
        ]
        for c in candidates:
            if os.path.exists(c):
                cfg_file = c
                break
    if cfg_file and os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    cfg_keys = data.get("provider_keys", {}) or {}
        except Exception:
            pass

    gemini = (
        env.get("GEMINI_API_KEY", "")
        or get_header("x-gemini-key")
        or get_header("X-Gemini-Key")
        or cfg_keys.get("gemini", "")
    ).strip()

    groq = (
        env.get("GROQ_API_KEY", "")
        or get_header("x-groq-key")
        or get_header("X-Groq-Key")
        or cfg_keys.get("groq", "")
    ).strip()

    openrouter = (
        env.get("OPENROUTER_API_KEY", "")
        or get_header("x-openrouter-key")
        or get_header("X-OpenRouter-Key")
        or cfg_keys.get("openrouter", "")
    ).strip()

    return ProviderKeys(
        gemini_key=gemini or None,
        groq_key=groq or None,
        openrouter_key=openrouter or None,
    )


def extract_provider_models(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Read configured model IDs from server config, falling back to shipped defaults."""
    if env is None:
        env = os.environ
    models = dict(DEFAULT_PROVIDER_MODELS)
    cfg_file = env.get("PANTRY_CONFIG_FILE", "")
    if not cfg_file and not env.get("PANTRY_TEST_MODE"):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(base_dir, "pantry_desktop_config.json"),
            os.path.join(os.path.dirname(base_dir), "pantry_desktop_config.json"),
            "pantry_desktop_config.json",
        ]
        for c in candidates:
            if os.path.exists(c):
                cfg_file = c
                break
    if cfg_file and os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    pm = data.get("provider_models")
                    if isinstance(pm, dict):
                        for k, v in pm.items():
                            k_clean = k.lower().strip()
                            if k_clean in models and v and isinstance(v, str) and v.strip():
                                models[k_clean] = v.strip()
        except Exception:
            pass
    return models


def save_configured_model(
    provider: str,
    model_id: str,
    env: Optional[Dict[str, str]] = None,
) -> None:
    """Save model ID into server config atomically."""
    if env is None:
        env = os.environ
    cfg_file = env.get("PANTRY_CONFIG_FILE", "")
    if not cfg_file and not env.get("PANTRY_TEST_MODE"):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(base_dir, "pantry_desktop_config.json"),
            os.path.join(os.path.dirname(base_dir), "pantry_desktop_config.json"),
            "pantry_desktop_config.json",
        ]
        for c in candidates:
            if os.path.exists(c):
                cfg_file = c
                break
    if not cfg_file:
        cfg_file = "pantry_desktop_config.json"

    data = {}
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    if "provider_models" not in data or not isinstance(data["provider_models"], dict):
        data["provider_models"] = dict(DEFAULT_PROVIDER_MODELS)
    data["provider_models"][provider.lower().strip()] = model_id.strip()

    cfg_path = Path(cfg_file)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(cfg_path.parent), prefix=".cfg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(cfg_path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _image_to_base64_data_url(image: Image.Image) -> str:
    buffered = io.BytesIO()
    fmt = image.format or "JPEG"
    if fmt.upper() not in ("JPEG", "JPG", "PNG", "WEBP"):
        fmt = "JPEG"
    if image.mode in ("RGBA", "P") and fmt.upper() in ("JPEG", "JPG"):
        image = image.convert("RGB")
    image.save(buffered, format=fmt)
    img_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
    mime = "image/jpeg" if fmt.upper() in ("JPEG", "JPG") else f"image/{fmt.lower()}"
    return f"data:{mime};base64,{img_b64}"


def _clean_json_markdown(text: str) -> str:
    """Strip markdown code fence blocks if returned by provider."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _call_gemini(
    api_key: str,
    contents: Any,
    schema: Type[BaseModel],
    temperature: float,
    timeout: float = 30.0,
    model: str = GEMINI_DEFAULT_MODEL,
) -> str:
    """Invoke Google Gemini using google.genai client."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
        ),
    )
    return getattr(response, "text", "") or ""


def _call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, Any]],
    temperature: float,
    timeout: float = 40.0,
    extra_headers: Optional[Dict[str, str]] = None,
) -> str:
    """Invoke an OpenAI-compatible chat completion endpoint."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("Provider returned no choices in response.")
    msg = choices[0].get("message", {})
    return msg.get("content", "") or ""


def _build_openai_messages(contents: Any, schema: Type[BaseModel]) -> List[Dict[str, Any]]:
    """Convert input contents (text, PIL images, or pdf bytes) to OpenAI-compatible messages with JSON schema instructions."""
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    system_instruction = (
        "You are an expert AI assistant that strictly outputs valid JSON matching the following schema. "
        "Do not include any conversational commentary, explanations, or Markdown code fences outside the JSON. "
        f"JSON Schema:\n{schema_json}"
    )

    user_parts: List[Dict[str, Any]] = []

    if isinstance(contents, str):
        user_parts.append({"type": "text", "text": contents})
    elif isinstance(contents, list):
        for item in contents:
            if isinstance(item, str):
                user_parts.append({"type": "text", "text": item})
            elif isinstance(item, Image.Image):
                data_url = _image_to_base64_data_url(item)
                user_parts.append({
                    "type": "image_url",
                    "image_url": {"url": data_url},
                })
            elif hasattr(item, "data") and hasattr(item, "mime_type"):
                # Handle google.genai types.Part or PDF bytes
                if "pdf" in getattr(item, "mime_type", "").lower():
                    # For PDF parts, notify that raw PDF is passed or convert if needed
                    pdf_b64 = base64.b64encode(item.data).decode("utf-8")
                    user_parts.append({
                        "type": "text",
                        "text": f"[Attached Document (PDF, base64 length {len(pdf_b64)})]"
                    })
            else:
                user_parts.append({"type": "text", "text": str(item)})
    else:
        user_parts.append({"type": "text", "text": str(contents)})

    return [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": user_parts},
    ]


def _is_retryable_error(exc: Exception) -> Tuple[bool, str]:
    """Check if an exception is an HTTP 503, 429, or connection/read timeout."""
    # Check requests exceptions
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True, f"Timeout or connection failure: {exc}"
    if isinstance(exc, requests.exceptions.HTTPError):
        code = getattr(exc.response, "status_code", None)
        if code in (429, 503, 502, 504):
            return True, f"HTTP {code} ({exc})"
        return False, f"HTTP {code} ({exc})"

    # Check google.genai or generic API exceptions by inspecting message / attributes
    msg = str(exc).lower()
    if "503" in msg or "service unavailable" in msg or "high demand" in msg:
        return True, f"503 Service Unavailable: {exc}"
    if "429" in msg or "quota" in msg or "rate limit" in msg or "resource exhausted" in msg:
        return True, f"429 Rate Limited: {exc}"
    if "timed out" in msg or "timeout" in msg:
        return True, f"Timeout: {exc}"

    # Also check if status_code or code attribute exists
    code = getattr(exc, "status_code", getattr(exc, "code", None))
    if code in (429, 503, 502, 504):
        return True, f"Status code {code}: {exc}"

    return False, str(exc)


def generate_structured_with_fallback(
    keys: ProviderKeys,
    contents: Any,
    schema: Type[BaseModel],
    temperature: float,
    what: str,
    env: Optional[Dict[str, str]] = None,
    item_count: Optional[int] = None,
    action: Optional[str] = None,
) -> Tuple[BaseModel, str]:
    """
    Executes a structured output AI call following the fallback chain:
      1. Gemini (if key provided)
      2. Groq (if key provided)
      3. OpenRouter (if key provided)

    Returns:
      (validated_schema_instance, serving_provider_name)

    Raises:
      RuntimeError with a user-friendly error message if all fail or no keys available.
    """
    # Build list of active candidates
    candidates: List[Tuple[str, str]] = []
    if keys.gemini_key:
        candidates.append(("gemini", keys.gemini_key))
    if keys.groq_key:
        candidates.append(("groq", keys.groq_key))
    if keys.openrouter_key:
        candidates.append(("openrouter", keys.openrouter_key))

    if not candidates:
        raise RuntimeError(
            "No AI provider keys configured. Please enter your Gemini API key (or Groq/OpenRouter fallback key) in Settings."
        )

    # Size-aware provider routing for plan generation:
    # When inventory is large (item_count > GROQ_GENERATION_MAX_ITEMS), Groq free tier truncates full plans.
    # For action == "generate_plan", remove Groq unless it is the only provider or removal would leave zero candidates.
    effective_action = action or ("generate_plan" if what == "build your weekly menu" else "")
    if effective_action == "generate_plan" and item_count is not None and item_count > GROQ_GENERATION_MAX_ITEMS:
        groq_candidates = [c for c in candidates if c[0] == "groq"]
        non_groq_candidates = [c for c in candidates if c[0] != "groq"]
        if groq_candidates and non_groq_candidates:
            logger.info(
                "AI route skip: provider='groq' reason='inventory_size' items=%d threshold=%d action='%s'",
                item_count, GROQ_GENERATION_MAX_ITEMS, effective_action
            )
            candidates = non_groq_candidates

    models = extract_provider_models(env)
    all_down = all(is_provider_down(p_name) for p_name, _ in candidates)
    if all_down and len(candidates) > 0:
        logger.info("All providers are marked DOWN in watchdog probe status; attempting all in normal order as fallback.")

    chain_start = time.time()
    last_errors: List[str] = []

    for provider_name, api_key in candidates:
        if not all_down and is_provider_down(provider_name):
            reason = get_provider_down_reason(provider_name)
            logger.info("Skipping provider %s: latest probe state is DOWN (%s)", provider_name, reason)
            continue

        active_model = models.get(
            provider_name,
            GEMINI_DEFAULT_MODEL if provider_name == "gemini" else (
                GROQ_DEFAULT_MODEL if provider_name == "groq" else OPENROUTER_DEFAULT_MODEL
            )
        )
        logger.info("Attempting AI request for '%s' using provider: %s (model: %s)", what, provider_name, active_model)
        is_primary = (provider_name == candidates[0][0])
        max_attempts = 2 if is_primary else 1

        for attempt in range(1, max_attempts + 1):
            attempt_start = time.time()
            raw_text = ""
            try:
                if provider_name == "gemini":
                    raw_text = _call_gemini(
                        api_key,
                        contents,
                        schema,
                        temperature,
                        model=active_model,
                    )
                elif provider_name == "groq":
                    messages = _build_openai_messages(contents, schema)
                    raw_text = _call_openai_compatible(
                        base_url=GROQ_BASE_URL,
                        api_key=api_key,
                        model=active_model,
                        messages=messages,
                        temperature=temperature,
                    )
                elif provider_name == "openrouter":
                    messages = _build_openai_messages(contents, schema)
                    raw_text = _call_openai_compatible(
                        base_url=OPENROUTER_BASE_URL,
                        api_key=api_key,
                        model=active_model,
                        messages=messages,
                        temperature=temperature,
                        extra_headers={
                            "HTTP-Referer": "https://pantryplanner.app",
                            "X-Title": "Pantry Planner",
                        },
                    )

                if not raw_text or not raw_text.strip():
                    duration_ms = int((time.time() - attempt_start) * 1000)
                    outcome = "retryable-failure" if (is_primary and attempt < max_attempts) else "failure"
                    logger.info(
                        "AI attempt timing: action='%s' provider='%s' model='%s' attempt=%d duration_ms=%d outcome='%s'",
                        what, provider_name, active_model, attempt, duration_ms, outcome
                    )
                    logger.warning("Provider %s returned empty response for '%s'", provider_name, what)
                    last_errors.append(f"{provider_name}: empty response")
                    continue

                cleaned_text = _clean_json_markdown(raw_text)
                parsed = schema.model_validate_json(cleaned_text)
                mark_provider_ok(provider_name, env=env)
                duration_ms = int((time.time() - attempt_start) * 1000)
                logger.info(
                    "AI attempt timing: action='%s' provider='%s' model='%s' attempt=%d duration_ms=%d outcome='success'",
                    what, provider_name, active_model, attempt, duration_ms
                )
                total_duration_ms = int((time.time() - chain_start) * 1000)
                logger.info(
                    "AI total timing: action='%s' duration_ms=%d outcome='success' served_by='%s'",
                    what, total_duration_ms, provider_name
                )
                logger.info("Successfully served '%s' via provider: %s", what, provider_name)
                return parsed, provider_name

            except Exception as exc:
                duration_ms = int((time.time() - attempt_start) * 1000)
                retryable, detail = _is_retryable_error(exc)
                # Schema validation failure: treat as malformed JSON and retryable
                if "validation" in str(type(exc)).lower() or "json" in str(type(exc)).lower():
                    retryable = True
                    detail = f"Malformed JSON output: {exc}"

                outcome = "retryable-failure" if (is_primary and retryable and attempt < max_attempts) else "failure"
                logger.info(
                    "AI attempt timing: action='%s' provider='%s' model='%s' attempt=%d duration_ms=%d outcome='%s'",
                    what, provider_name, active_model, attempt, duration_ms, outcome
                )

                # Primary gets ONE same-provider retry after a short backoff (2-4s)
                if outcome == "retryable-failure":
                    logger.warning(
                        "Primary provider %s encountered retryable error for '%s' (attempt %d/%d): %s. Retrying in 2.5s...",
                        provider_name, what, attempt, max_attempts, detail
                    )
                    time.sleep(2.5)
                    continue

                logger.warning(
                    "Provider %s failed for '%s' (retryable=%s): %s",
                    provider_name, what, retryable, detail
                )
                last_errors.append(f"{provider_name} ({detail})")
                if not retryable and len(candidates) == 1:
                    # If only one provider and hard non-retryable error, still fail gracefully
                    break

    # If all configured providers failed
    total_duration_ms = int((time.time() - chain_start) * 1000)
    logger.info(
        "AI total timing: action='%s' duration_ms=%d outcome='failure'",
        what, total_duration_ms
    )
    name_map = {"gemini": "Gemini", "groq": "Groq", "openrouter": "OpenRouter"}
    tried_names = [name_map.get(p.lower(), p.capitalize()) for p, _ in candidates]
    action_prefix = f"Unable to {what}: " if what else ""
    if len(tried_names) > 1:
        friendly_msg = f"{action_prefix}All AI services are unavailable right now — {', '.join(tried_names)} were all tried. Please try again in a few minutes."
    else:
        friendly_msg = f"{action_prefix}AI service ({tried_names[0]}) is unavailable right now. Please try again in a few minutes."
    logger.error("All AI providers in fallback chain failed for '%s': %s", what, "; ".join(last_errors))
    raise RuntimeError(friendly_msg)


def validate_provider_key(provider: str, api_key: str) -> Tuple[bool, str]:
    """Test a provider key with a tiny request."""
    provider = provider.lower().strip()
    api_key = (api_key or "").strip()
    if not api_key:
        return False, "No key provided."

    try:
        if provider == "gemini":
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            client.models.generate_content(
                model=GEMINI_DEFAULT_MODEL,
                contents="ping",
                config=types.GenerateContentConfig(max_output_tokens=16, temperature=0),
            )
            return True, "Key works! You're connected."

        elif provider == "groq":
            url = f"{GROQ_BASE_URL.rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": GROQ_DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=15.0)
            if resp.status_code == 401:
                return False, "Groq rejected the API key (401 Unauthorized)."
            if resp.status_code == 200:
                return True, "Groq key works!"
            # If the model is not accessible or returns another status, check /models to verify key validity
            models_resp = requests.get(
                f"{GROQ_BASE_URL.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15.0,
            )
            if models_resp.status_code == 401:
                return False, "Groq rejected the API key (401 Unauthorized)."
            if models_resp.status_code == 200:
                return True, "Groq key works!"
            resp.raise_for_status()
            return True, "Groq key works!"

        elif provider == "openrouter":
            url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://pantryplanner.app",
                "X-Title": "Pantry Planner",
            }
            payload = {
                "model": OPENROUTER_DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=15.0)
            if resp.status_code == 401:
                return False, "OpenRouter rejected the API key (401 Unauthorized)."
            if resp.status_code == 200:
                return True, "OpenRouter key works!"
            # If model endpoint is unavailable, verify key directly via auth/key or models
            auth_resp = requests.get(
                f"{OPENROUTER_BASE_URL.rstrip('/')}/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15.0,
            )
            if auth_resp.status_code == 401:
                return False, "OpenRouter rejected the API key (401 Unauthorized)."
            if auth_resp.status_code == 200:
                return True, "OpenRouter key works!"
            models_resp = requests.get(
                f"{OPENROUTER_BASE_URL.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15.0,
            )
            if models_resp.status_code == 401:
                return False, "OpenRouter rejected the API key (401 Unauthorized)."
            if models_resp.status_code == 200:
                return True, "OpenRouter key works!"
            resp.raise_for_status()
            return True, "OpenRouter key works!"

        else:
            return False, f"Unknown provider: {provider}"

    except Exception as e:
        return False, f"Provider rejected the key: {e}"


# ------------------------------------------------------------------
# Provider Resilience: Self-Probe, Auto-Discovery & Status Watchdog
# ------------------------------------------------------------------

_PROVIDER_STATUS_CACHE: Dict[str, Dict[str, Any]] = {}


def _get_status_file_path(env: Optional[Dict[str, str]] = None) -> Path:
    if env is None:
        env = os.environ
    cfg_file = env.get("PANTRY_CONFIG_FILE", "")
    if cfg_file:
        return Path(cfg_file).parent / "pantry_providers_status.json"
    if env.get("PANTRY_TEST_MODE"):
        return Path(tempfile.gettempdir()) / "pantry_providers_status_test.json"
    base_dir = Path(__file__).resolve().parent
    return base_dir / "pantry_providers_status.json"


def _load_status_cache_from_disk(env: Optional[Dict[str, str]] = None) -> None:
    path = _get_status_file_path(env)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                providers = data.get("providers")
                if isinstance(providers, dict):
                    _PROVIDER_STATUS_CACHE.clear()
                    _PROVIDER_STATUS_CACHE.update(providers)
        except Exception:
            pass


def _save_status_cache_to_disk(env: Optional[Dict[str, str]] = None) -> None:
    path = _get_status_file_path(env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "providers": _PROVIDER_STATUS_CACHE,
            "last_checked": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def is_provider_down(provider: str) -> bool:
    """Check if the latest probe state for the provider is DOWN."""
    entry = _PROVIDER_STATUS_CACHE.get(provider.lower().strip())
    if not entry:
        return False
    return entry.get("state") == "down"


def get_provider_down_reason(provider: str) -> str:
    """Return the error message or reason why provider is marked down."""
    entry = _PROVIDER_STATUS_CACHE.get(provider.lower().strip())
    if not entry:
        return "provider down"
    return entry.get("error") or "probe marked DOWN"


def mark_provider_ok(provider: str, env: Optional[Dict[str, str]] = None) -> None:
    """Clear DOWN state immediately upon real call success."""
    p = provider.lower().strip()
    now_dt = datetime.now()
    now_iso = now_dt.isoformat()
    now_display = now_dt.strftime("%I:%M %p").lstrip("0")
    models = extract_provider_models(env)
    name_map = {"gemini": "Gemini", "groq": "Groq", "openrouter": "OpenRouter"}
    if p not in _PROVIDER_STATUS_CACHE:
        _PROVIDER_STATUS_CACHE[p] = {
            "provider": p,
            "name": name_map.get(p, p.capitalize()),
            "model": models.get(p, ""),
            "state": "ok",
            "status_word": "Working",
            "status_code": 200,
            "error": None,
            "auto_updated_msg": None,
            "checked_at": now_iso,
            "checked_display": now_display,
        }
    else:
        _PROVIDER_STATUS_CACHE[p]["state"] = "ok"
        _PROVIDER_STATUS_CACHE[p]["status_word"] = "Working"
        _PROVIDER_STATUS_CACHE[p]["error"] = None
        _PROVIDER_STATUS_CACHE[p]["checked_at"] = now_iso
        _PROVIDER_STATUS_CACHE[p]["checked_display"] = now_display
    _save_status_cache_to_disk(env)


def is_vision_model(model_info: Any) -> bool:
    """Shared helper to determine if a model is vision-capable."""
    if isinstance(model_info, dict):
        arch = model_info.get("architecture")
        if isinstance(arch, dict):
            input_mods = arch.get("input_modalities") or []
            if "image" in input_mods:
                return True
            modality = str(arch.get("modality", "")).lower()
            if "image" in modality:
                return True
        model_id = str(model_info.get("id", "")).lower()
    else:
        model_id = str(model_info or "").lower()

    if not model_id:
        return False

    # Negative patterns: text-only, whisper, guard, etc.
    text_only = ["llama-3.1-8b", "llama-3.3-70b", "whisper", "guard", "orpheus", "allam", "qwen-2.5-72b"]
    for t in text_only:
        if t in model_id:
            return False

    # Vision markers
    vision_keywords = ["vision", "scout", "-vl", ":vl", "super-vl", "qwen3.8", "flash", "gemini-2", "gemini-1.5"]
    return any(k in model_id for k in vision_keywords)


def auto_discover_substitute(provider: str, available_models: List[Dict[str, Any]]) -> Optional[str]:
    """Select a vision-capable substitute when configured model is absent.
    Groq: Llama vision/Scout line first, then any other vision model.
    OpenRouter: Gemini Flash :free first, then any :free vision model, then other vision.
    Gemini: NEVER auto-substituted (returns None).
    Deterministic order (sorted by ID), first match wins.
    """
    provider = provider.lower().strip()
    if provider == "gemini":
        return None  # Gemini (primary) is NEVER auto-substituted!

    if provider == "groq":
        vision_models = [m for m in available_models if is_vision_model(m)]
        if not vision_models:
            return None
        # Preference 1: Llama vision / Scout line
        scout_family = [
            m for m in vision_models
            if "llama" in str(m.get("id", "")).lower() and ("vision" in str(m.get("id", "")).lower() or "scout" in str(m.get("id", "")).lower())
        ]
        other_vision = [m for m in vision_models if m not in scout_family]
        scout_family.sort(key=lambda x: str(x.get("id", "")))
        other_vision.sort(key=lambda x: str(x.get("id", "")))
        ordered = scout_family + other_vision
        return ordered[0]["id"] if ordered else None

    if provider == "openrouter":
        vision_models = [m for m in available_models if is_vision_model(m)]
        if not vision_models:
            return None
        # Preference 1: Gemini Flash :free
        gemini_free = [
            m for m in vision_models
            if "google/gemini-" in str(m.get("id", "")).lower() and "-flash" in str(m.get("id", "")).lower() and ":free" in str(m.get("id", "")).lower()
        ]
        # Preference 2: any other :free vision model
        any_free = [
            m for m in vision_models
            if ":free" in str(m.get("id", "")).lower() and m not in gemini_free
        ]
        # Preference 3: other vision
        other_vision = [m for m in vision_models if m not in gemini_free and m not in any_free]
        gemini_free.sort(key=lambda x: str(x.get("id", "")))
        any_free.sort(key=lambda x: str(x.get("id", "")))
        other_vision.sort(key=lambda x: str(x.get("id", "")))
        ordered = gemini_free + any_free + other_vision
        return ordered[0]["id"] if ordered else None

    return None


def _probe_minimal_completion(provider: str, model: str, api_key: str) -> Tuple[bool, Optional[int], Optional[str]]:
    """Execute ONE minimal text completion (a few tokens) to prove key + endpoint health."""
    provider = provider.lower().strip()
    try:
        if provider == "gemini":
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            client.models.generate_content(
                model=model,
                contents="ping",
                config=types.GenerateContentConfig(max_output_tokens=2, temperature=0),
            )
            return True, 200, None
        elif provider == "groq":
            url = f"{GROQ_BASE_URL.rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 2}
            r = requests.post(url, headers=headers, json=payload, timeout=10.0)
            if r.status_code == 200:
                return True, 200, None
            return False, r.status_code, r.text[:200]
        elif provider == "openrouter":
            url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://pantryplanner.app",
                "X-Title": "Pantry Planner",
            }
            payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 2}
            r = requests.post(url, headers=headers, json=payload, timeout=10.0)
            if r.status_code == 200:
                return True, 200, None
            return False, r.status_code, r.text[:200]
        else:
            return False, 400, f"Unknown provider: {provider}"
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return False, status_code, str(exc)


def probe_provider(
    provider: str,
    model: Optional[str] = None,
    keys: Optional[ProviderKeys] = None,
    env: Optional[Dict[str, str]] = None,
    allow_substitute: bool = True,
) -> Dict[str, Any]:
    """Execute a health probe for one provider.
    (a) For OpenAI-compatible (Groq, OpenRouter): check configured model exists in /models.
    (b) For all three: execute ONE minimal text completion.
    Returns status dictionary.
    """
    provider = provider.lower().strip()
    name_map = {"gemini": "Gemini", "groq": "Groq", "openrouter": "OpenRouter"}
    name = name_map.get(provider, provider.capitalize())
    models = extract_provider_models(env)
    configured_model = model or models.get(provider, DEFAULT_PROVIDER_MODELS.get(provider, ""))

    if keys is None:
        keys = extract_provider_keys(headers={}, env=env)

    api_key = getattr(keys, f"{provider}_key", None)
    now_dt = datetime.now()
    now_iso = now_dt.isoformat()
    now_display = now_dt.strftime("%I:%M %p").lstrip("0")

    if not api_key:
        result = {
            "provider": provider,
            "name": name,
            "model": configured_model,
            "state": "down",
            "status_word": "Down",
            "status_code": None,
            "error": f"No API key configured for {name}",
            "auto_updated_msg": None,
            "checked_at": now_iso,
            "checked_display": now_display,
        }
        _PROVIDER_STATUS_CACHE[provider] = result
        _save_status_cache_to_disk(env)
        return result

    # 1. For OpenAI-compatible: GET /models and check if model is present
    if provider in ("groq", "openrouter"):
        base_url = GROQ_BASE_URL if provider == "groq" else OPENROUTER_BASE_URL
        models_url = f"{base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        if provider == "openrouter":
            headers.update({
                "HTTP-Referer": "https://pantryplanner.app",
                "X-Title": "Pantry Planner",
            })
        try:
            resp = requests.get(models_url, headers=headers, timeout=10.0)
            if resp.status_code != 200:
                result = {
                    "provider": provider,
                    "name": name,
                    "model": configured_model,
                    "state": "down",
                    "status_word": "Down",
                    "status_code": resp.status_code,
                    "error": f"Failed to list models: HTTP {resp.status_code}",
                    "auto_updated_msg": None,
                    "checked_at": now_iso,
                    "checked_display": now_display,
                }
                _PROVIDER_STATUS_CACHE[provider] = result
                _save_status_cache_to_disk(env)
                return result

            data = resp.json()
            available = data.get("data", []) if isinstance(data, dict) else []
            model_ids = [m.get("id", "") for m in available if isinstance(m, dict)]

            if configured_model not in model_ids:
                # Configured model is missing!
                if allow_substitute:
                    substitute = auto_discover_substitute(provider, available)
                    if substitute:
                        old_model = configured_model
                        # Persist substitution
                        save_configured_model(provider, substitute, env=env)
                        auto_msg = f"{name} model updated automatically: {old_model} → {substitute} (old model retired by provider)"
                        logger.info(auto_msg)

                        # Re-probe substitute with minimal completion
                        comp_ok, comp_code, comp_err = _probe_minimal_completion(provider, substitute, api_key)
                        if comp_ok:
                            result = {
                                "provider": provider,
                                "name": name,
                                "model": substitute,
                                "state": "ok",
                                "status_word": "Updated automatically",
                                "status_code": comp_code or 200,
                                "error": None,
                                "auto_updated_msg": auto_msg,
                                "checked_at": now_iso,
                                "checked_display": now_display,
                            }
                            _PROVIDER_STATUS_CACHE[provider] = result
                            _save_status_cache_to_disk(env)
                            return result
                        else:
                            # Substitute failed! Revert to original configured model
                            save_configured_model(provider, old_model, env=env)
                            result = {
                                "provider": provider,
                                "name": name,
                                "model": old_model,
                                "state": "down",
                                "status_word": "Down",
                                "status_code": comp_code,
                                "error": f"Substitute model '{substitute}' failed probe completion test: {comp_err}",
                                "auto_updated_msg": None,
                                "checked_at": now_iso,
                                "checked_display": now_display,
                            }
                            _PROVIDER_STATUS_CACHE[provider] = result
                            _save_status_cache_to_disk(env)
                            return result
                    else:
                        # No substitute found
                        result = {
                            "provider": provider,
                            "name": name,
                            "model": configured_model,
                            "state": "down",
                            "status_word": "Down",
                            "status_code": 404,
                            "error": f"Model '{configured_model}' not found in {name} /models and no substitute found",
                            "auto_updated_msg": None,
                            "checked_at": now_iso,
                            "checked_display": now_display,
                        }
                        _PROVIDER_STATUS_CACHE[provider] = result
                        _save_status_cache_to_disk(env)
                        return result
                else:
                    # Not allowing substitute (e.g. manual model validation)
                    result = {
                        "provider": provider,
                        "name": name,
                        "model": configured_model,
                        "state": "down",
                        "status_word": "Down",
                        "status_code": 404,
                        "error": f"Model '{configured_model}' not found in {name} /models",
                        "auto_updated_msg": None,
                        "checked_at": now_iso,
                        "checked_display": now_display,
                    }
                    _PROVIDER_STATUS_CACHE[provider] = result
                    _save_status_cache_to_disk(env)
                    return result
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            result = {
                "provider": provider,
                "name": name,
                "model": configured_model,
                "state": "down",
                "status_word": "Down",
                "status_code": code,
                "error": str(exc),
                "auto_updated_msg": None,
                "checked_at": now_iso,
                "checked_display": now_display,
            }
            _PROVIDER_STATUS_CACHE[provider] = result
            _save_status_cache_to_disk(env)
            return result

    # 2. Minimal text completion
    comp_ok, comp_code, comp_err = _probe_minimal_completion(provider, configured_model, api_key)
    if comp_ok:
        result = {
            "provider": provider,
            "name": name,
            "model": configured_model,
            "state": "ok",
            "status_word": "Working",
            "status_code": comp_code or 200,
            "error": None,
            "auto_updated_msg": None,
            "checked_at": now_iso,
            "checked_display": now_display,
        }
    else:
        result = {
            "provider": provider,
            "name": name,
            "model": configured_model,
            "state": "down",
            "status_word": "Down",
            "status_code": comp_code,
            "error": f"Completion failed: {comp_err}",
            "auto_updated_msg": None,
            "checked_at": now_iso,
            "checked_display": now_display,
        }

    _PROVIDER_STATUS_CACHE[provider] = result
    _save_status_cache_to_disk(env)
    return result


def probe_all_providers(
    keys: Optional[ProviderKeys] = None,
    env: Optional[Dict[str, str]] = None,
    allow_substitute: bool = True,
) -> Dict[str, Any]:
    """Probe all 3 providers and persist results."""
    for provider in ("gemini", "groq", "openrouter"):
        probe_provider(provider, keys=keys, env=env, allow_substitute=allow_substitute)
    return get_providers_status(client_is_phone=False, env=env)


def get_providers_status(
    client_is_phone: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Return status dictionary.
    For phone clients, model IDs and sensitive internals are stripped.
    """
    if not _PROVIDER_STATUS_CACHE:
        _load_status_cache_from_disk(env)

    models = extract_provider_models(env)
    name_map = {"gemini": "Gemini", "groq": "Groq", "openrouter": "OpenRouter"}
    for p in ("gemini", "groq", "openrouter"):
        if p not in _PROVIDER_STATUS_CACHE:
            _PROVIDER_STATUS_CACHE[p] = {
                "provider": p,
                "name": name_map.get(p, p.capitalize()),
                "model": models.get(p, ""),
                "state": "ok",
                "status_word": "Working",
                "status_code": 200,
                "error": None,
                "auto_updated_msg": None,
                "checked_at": None,
                "checked_display": "Not checked yet",
            }

    if client_is_phone:
        phone_providers = {}
        for p, d in _PROVIDER_STATUS_CACHE.items():
            phone_providers[p] = {
                "name": d.get("name", name_map.get(p, p.capitalize())),
                "state": d.get("state", "ok"),
                "status_word": "Working" if d.get("state") == "ok" else "Down",
            }
        return {
            "providers": phone_providers,
            "last_checked": datetime.now(timezone.utc).isoformat(),
        }

    return {
        "providers": dict(_PROVIDER_STATUS_CACHE),
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }


def save_provider_model_with_validation(
    provider: str,
    candidate_model: str,
    keys: Optional[ProviderKeys] = None,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Validates candidate model via probe without auto-discovery.
    If valid, saves into config and updates status cache.
    If invalid (404 / not in list), refuses with exact required message.
    """
    provider = provider.lower().strip()
    candidate_model = candidate_model.strip()
    name_map = {"gemini": "Gemini", "groq": "Groq", "openrouter": "OpenRouter"}
    name = name_map.get(provider, provider.capitalize())

    if not candidate_model:
        return False, f"Model name for {name} cannot be empty.", {}

    probe_res = probe_provider(
        provider=provider,
        model=candidate_model,
        keys=keys,
        env=env,
        allow_substitute=False,  # strictly probe candidate model
    )

    if probe_res.get("state") != "ok":
        err_msg = f"That model isn't available from {name} right now — check the name and try again."
        return False, err_msg, probe_res

    save_configured_model(provider, candidate_model, env=env)
    return True, f"{name} model updated to {candidate_model}.", probe_res

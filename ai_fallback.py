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
import io
import json
import logging
import os
import re
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

PROVIDER_ORDER = ["gemini", "groq", "openrouter"]


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
) -> str:
    """Invoke Google Gemini using google.genai client."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_DEFAULT_MODEL,
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

    last_errors: List[str] = []

    for provider_name, api_key in candidates:
        logger.info("Attempting AI request for '%s' using provider: %s", what, provider_name)
        is_primary = (provider_name == candidates[0][0])
        max_attempts = 2 if is_primary else 1

        for attempt in range(1, max_attempts + 1):
            raw_text = ""
            try:
                if provider_name == "gemini":
                    raw_text = _call_gemini(api_key, contents, schema, temperature)
                elif provider_name == "groq":
                    messages = _build_openai_messages(contents, schema)
                    raw_text = _call_openai_compatible(
                        base_url=GROQ_BASE_URL,
                        api_key=api_key,
                        model=GROQ_DEFAULT_MODEL,
                        messages=messages,
                        temperature=temperature,
                    )
                elif provider_name == "openrouter":
                    messages = _build_openai_messages(contents, schema)
                    raw_text = _call_openai_compatible(
                        base_url=OPENROUTER_BASE_URL,
                        api_key=api_key,
                        model=OPENROUTER_DEFAULT_MODEL,
                        messages=messages,
                        temperature=temperature,
                        extra_headers={
                            "HTTP-Referer": "https://pantryplanner.app",
                            "X-Title": "Pantry Planner",
                        },
                    )

                if not raw_text or not raw_text.strip():
                    logger.warning("Provider %s returned empty response for '%s'", provider_name, what)
                    last_errors.append(f"{provider_name}: empty response")
                    continue

                cleaned_text = _clean_json_markdown(raw_text)
                parsed = schema.model_validate_json(cleaned_text)
                logger.info("Successfully served '%s' via provider: %s", what, provider_name)
                return parsed, provider_name

            except Exception as exc:
                retryable, detail = _is_retryable_error(exc)
                # Schema validation failure: treat as malformed JSON and retryable
                if "validation" in str(type(exc)).lower() or "json" in str(type(exc)).lower():
                    retryable = True
                    detail = f"Malformed JSON output: {exc}"

                # Primary gets ONE same-provider retry after a short backoff (2-4s)
                if is_primary and retryable and attempt < max_attempts:
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

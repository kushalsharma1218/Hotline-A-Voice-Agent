"""Minimal Gemini API client (generateContent over HTTPS, standard library only).

    client = GeminiClient(api_key)
    response = client.generate("gemini-3.8-flash", body)   # body from request.build_request

Retries mirror the n8n "Call Gemini" HTTP node: 5xx and timeouts are retried
``max_retries`` times, 5 s apart. 429 (the free tier's per-minute quota) is
retried up to ``max_rate_limit_retries`` times, waiting for the delay the API
suggests (``RetryInfo``) or ``RATE_LIMIT_WAIT_S``.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
RETRY_WAIT_S = 5.0
RATE_LIMIT_WAIT_S = 20.0


class GeminiAPIError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"{status or 'network'}: {message}")
        self.status = status


def api_key_from_env() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def endpoint(model: str, base_url: str = BASE_URL) -> str:
    return f"{base_url}/models/{urllib.parse.quote(model, safe='')}:generateContent"


def _retry_delay(body: str) -> float | None:
    """Seconds from a google.rpc.RetryInfo detail (e.g. "17s"), if present."""
    try:
        details = json.loads(body)["error"].get("details", [])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    for d in details:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(d.get("retryDelay", "")))
        if m:
            return float(m.group(1))
    return None


class GeminiClient:
    def __init__(self, api_key: str, max_retries: int = 2, max_rate_limit_retries: int = 5,
                 timeout: float = 60.0, base_url: str = BASE_URL):
        self.api_key = api_key
        self.max_retries = max_retries
        self.max_rate_limit_retries = max_rate_limit_retries
        self.timeout = timeout
        self.base_url = base_url

    def generate(self, model: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        errors = rate_limited = 0
        while True:
            req = urllib.request.Request(
                endpoint(model, self.base_url), data=data, method="POST",
                headers={"content-type": "application/json", "x-goog-api-key": self.api_key})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                if exc.code == 429 and rate_limited < self.max_rate_limit_retries:
                    rate_limited += 1
                    time.sleep(_retry_delay(text) or RATE_LIMIT_WAIT_S)
                    continue
                if exc.code >= 500 and errors < self.max_retries:
                    errors += 1
                    time.sleep(RETRY_WAIT_S)
                    continue
                raise GeminiAPIError(exc.code, text[:900]) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if errors < self.max_retries:
                    errors += 1
                    time.sleep(RETRY_WAIT_S)
                    continue
                raise GeminiAPIError(None, str(exc)) from None

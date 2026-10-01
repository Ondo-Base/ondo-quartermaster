"""Hosted decision-model adapter (Jev, TypeSafe AI), called through OpenRouter.

Jev is closed and hosted only, so on paths that carry customer file contents or
screen text it is a third party in the data flow, and through OpenRouter so is
OpenRouter. It is the cloud-tier implementation; the on-prem tier needs a
self-hosted model behind the same interface before screening can be sold
locally (docs/design.md §5, docs/decision-local.md).

The defaults are OpenRouter's System One API, per OpenRouter's documentation
(openrouter.ai/docs/api/api-reference/systemone/submit-a-system-one-request):
``POST https://openrouter.ai/api/v1/systemone`` with model ``typesafe/jev-1.13``,
authenticated with the same ``OPENROUTER_API_KEY`` the orchestrator profiles
use. The request and answers are the ``/v1/systemone`` shape in
``systemone.py``, shared with the self-hosted Laya adapter. OpenRouter's
OpenAI-compatible chat endpoint does not serve Jev, so ``/v1/chat/completions``
is the wrong path. ``base_url``, ``path`` and ``model`` stay configurable for a
direct TypeSafe account; ``~typesafe/jev-latest`` is OpenRouter's moving alias,
but a pinned version keeps calibrated thresholds meaningful.
"""

from __future__ import annotations

import os

import httpx

from . import systemone
from .interface import Answer, Question

OPENROUTER_BASE_URL = "https://openrouter.ai/api"
SYSTEMONE_PATH = "/v1/systemone"
# Pinned, not the ~typesafe/jev-latest alias: thresholds are measured per model.
DEFAULT_MODEL = "typesafe/jev-1.13"


class JevDecisionModel:
    name = "jev"

    def __init__(
        self,
        *,
        base_url: str = OPENROUTER_BASE_URL,
        path: str = SYSTEMONE_PATH,
        model: str = DEFAULT_MODEL,
        api_key_env: str = "OPENROUTER_API_KEY",
        timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ):
        if not base_url:
            raise ValueError("Jev base_url is empty; leave it unset for OpenRouter")
        self.url = base_url.rstrip("/") + ("/" + path.lstrip("/") if path else "")
        self.model = model
        self.api_key = os.environ.get(api_key_env, "")
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    async def ask(self, state: str, questions: list[Question]) -> list[Answer]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            r = await self._client.post(
                self.url, json=systemone.to_request(state, questions, self.model), headers=headers
            )
            r.raise_for_status()
            return systemone.from_response(questions, r.json())
        except (httpx.HTTPError, ValueError):
            # Decision failures never block and never permit: every question
            # answers "uncertain", which the gate escalates to a person.
            return systemone.uncertain(questions, error=1.0)

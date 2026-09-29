"""Hosted decision-model adapter (Jev, TypeSafe AI).

Jev is closed and hosted only, so on paths that carry customer file contents or
screen text it is a third party in the data flow. It is the cloud-tier
implementation; the on-prem tier needs a self-hosted model behind the same
interface before screening can be sold locally (implementation plan §5, Stage 6.5).

OPEN QUESTION, carried from the plan: published sources disagree on the endpoint
(``api.typesafe.ai/v1/systemone`` vs a docs mirror on a domain TypeSafe does not
own). Nothing here hard-codes an endpoint: ``base_url`` and ``path`` come from
configuration. The request and answers are the ``/v1/systemone`` shape in
``systemone.py``, shared with the self-hosted Laya adapter; Laya documents it as
Jev's, but it has not been checked against TypeSafe's own docs. Do not point this
at the mirror.
"""

from __future__ import annotations

import os

import httpx

from . import systemone
from .interface import Answer, Question


class JevDecisionModel:
    name = "jev"

    def __init__(
        self,
        *,
        base_url: str,
        path: str = "",
        model: str = "jev",
        api_key_env: str = "JEV_API_KEY",
        timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ):
        if not base_url:
            raise ValueError("Jev base_url must be configured from the official docs; see module docstring")
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

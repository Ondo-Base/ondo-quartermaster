"""Self-hosted decision-model adapter (Laya, Convai Innovations, Apache 2.0).

The on-prem tier (implementation plan Stage 6.5): the same typed questions as
Jev, answered by a model that runs inside the customer's network. Two ways to
run it, and neither can reach the internet:

- ``endpoint``: a ``laya-serve`` the customer hosts (``POST /v1/systemone``, the
  same wire as Jev, see ``systemone.py``). The adapter refuses an endpoint that
  is not on the customer's network: a loopback or private address, or a host
  the configuration names in ``internal_hosts``. It never uses a proxy from the
  environment, since a proxy would carry the call out.
- ``checkpoint``: a local directory of Laya weights, loaded in this process
  (``pip install "ondo-agent[laya]"``; ``onnx`` for the ONNX Runtime graph). It
  must be a directory on disk, never a Hub id, and the Hugging Face libraries
  are put in offline mode before they load.

The shipped checkpoints are near chance on typed decisions zero-shot (Laya's own
figure: 0.362, below the majority-class baseline). Fine-tune on this product's
labelled decisions (``ondo-agent decisions export``) and measure against the
bar (``python -m ondo_agent.decision.calibrate --bar``) before trusting one.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import systemone
from .interface import Answer, Question


class OffNetwork(ValueError):
    """The configured endpoint would take decisions outside the customer's network."""


def _internal_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_loopback or a.is_private or a.is_link_local


def check_on_network(url: str, internal_hosts: list[str] | tuple[str, ...] = ()) -> str:
    """Return ``url`` if its host is inside the customer's network, else raise OffNetwork.

    A host is inside when the configuration names it (``laya.corp``, or a suffix
    written ``.corp``), or when it is, or resolves only to, loopback, private or
    link-local addresses. Checked when the adapter is built.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise OffNetwork(f"decision endpoint {url!r} is not an http(s) URL")
    for h in internal_hosts:
        h = h.lower()
        if host == h or (h.startswith(".") and host.endswith(h)):
            return url
    try:
        addrs = [host] if _is_ip(host) else [ai[4][0] for ai in socket.getaddrinfo(host, parts.port or 443)]
    except OSError as e:
        raise OffNetwork(f"decision endpoint host {host!r} does not resolve: {e}") from e
    outside = sorted({str(a) for a in addrs if not _internal_ip(str(a))})
    if not addrs or outside:
        raise OffNetwork(
            f"decision endpoint {host!r} is outside the customer's network ({', '.join(outside) or 'no address'}). "
            "Use a private address, or name the host in decision.internal_hosts."
        )
    return url


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%")[0])
        return True
    except ValueError:
        return False


def load_checkpoint(path: str, *, onnx: str = "", calibration: str = "", device: str = "") -> Any:
    """Load Laya from a local directory, offline. Returns an object with ``system_one``."""
    p = Path(path).expanduser()
    if not p.is_dir():
        raise FileNotFoundError(f"Laya checkpoint {path!r} is not a local directory; Hub ids are not loaded")
    # Before the first import: the Hugging Face libraries read these once.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    kw: dict[str, Any] = {"calibration": calibration} if calibration else {}
    if onnx:
        from laya.onnx_agent import ONNXAgent

        return ONNXAgent(str(p), onnx_path=str(Path(onnx).expanduser()), **kw)
    import laya

    return laya.load(str(p), device=device or None, **kw)


class LayaDecisionModel:
    name = "laya"

    def __init__(
        self,
        *,
        endpoint: str = "",
        path: str = "/v1/systemone",
        checkpoint: str = "",
        onnx: str = "",
        calibration: str = "",
        device: str = "",
        model: str = "",
        internal_hosts: list[str] | tuple[str, ...] = (),
        api_key_env: str = "LAYA_API_KEY",
        timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
        agent: Any = None,
    ):
        if sum(bool(x) for x in (endpoint, checkpoint, agent is not None)) != 1:
            raise ValueError("configure exactly one of decision.endpoint or decision.checkpoint for Laya")
        self.model = model or None
        self._agent = agent
        self._load_args = {"onnx": onnx, "calibration": calibration, "device": device} if checkpoint else None
        self._checkpoint = checkpoint
        self._lock = asyncio.Lock()
        self.url = ""
        if endpoint:
            base = check_on_network(endpoint, internal_hosts)
            self.url = base.rstrip("/") + ("/" + path.lstrip("/") if path else "")
            self.api_key = os.environ.get(api_key_env, "")
            # trust_env=False: no proxy or netrc from the environment. A proxy
            # would carry screen text out of the network this check just proved.
            self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), trust_env=False)
        elif checkpoint and not Path(checkpoint).expanduser().is_dir():
            raise FileNotFoundError(f"Laya checkpoint {checkpoint!r} is not a local directory; Hub ids are not loaded")

    async def _local(self) -> Any:
        async with self._lock:
            if self._agent is None:
                assert self._load_args is not None
                self._agent = await asyncio.to_thread(load_checkpoint, self._checkpoint, **self._load_args)
        return self._agent

    async def ask(self, state: str, questions: list[Question]) -> list[Answer]:
        if not questions:
            return []
        body = systemone.to_request(state, questions, self.model)
        try:
            if self.url:
                headers = {"content-type": "application/json"}
                if self.api_key:
                    headers["authorization"] = f"Bearer {self.api_key}"
                r = await self._client.post(self.url, json=body, headers=headers)
                r.raise_for_status()
                data = r.json()
            else:
                agent = await self._local()
                data = await asyncio.to_thread(agent.system_one, body["state"], body["questions"])
            return systemone.from_response(questions, data)
        # As with Jev: a failure never blocks and never permits. Every question
        # answers "uncertain", which the gate sends to a person. In process,
        # that covers whatever the model runtime raises.
        except (httpx.HTTPError, ValueError):
            return systemone.uncertain(questions, error=1.0)
        except Exception:  # noqa: BLE001
            if self.url:
                raise
            return systemone.uncertain(questions, error=1.0)

"""Thin REST client for the two AI Core deployments this stage uses: the
orchestration deployment (LLM completion) and the embedding deployment
(text-embedding-3-small). Real AI Core Generative AI Hub calls -- no proxying.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests

from pipeline.rag import config

_token_cache: dict = {"token": None, "expires_at": 0.0}


def _load_ai_core_creds() -> dict:
    with open(config.CREDENTIALS_PATH) as f:
        return json.load(f)["ai_core"]


def _get_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    creds = _load_ai_core_creds()
    resp = requests.post(
        creds["auth_url"] + "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        },
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    _token_cache["token"] = body["access_token"]
    _token_cache["expires_at"] = time.time() + body.get("expires_in", 3600) - 60
    return _token_cache["token"]


def _deployment_url(deployment_id: str) -> str:
    creds = _load_ai_core_creds()
    return f"{creds['api_url'].rstrip('/')}/v2/inference/deployments/{deployment_id}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "AI-Resource-Group": config.AI_CORE_RESOURCE_GROUP,
        "Content-Type": "application/json",
    }


def embed_texts(texts: list[str], batch_size: int = 16) -> list[list[float]]:
    """Embed a list of strings via the text-embedding-3-small deployment.
    Batches requests since Azure OpenAI embedding endpoints cap batch size."""
    url = _deployment_url(config.EMBEDDING_DEPLOYMENT_ID) + "/embeddings?api-version=2023-05-15"
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = requests.post(url, headers=_headers(), json={"input": batch}, timeout=60)
        resp.raise_for_status()
        data = resp.json()["data"]
        out.extend(item["embedding"] for item in sorted(data, key=lambda d: d["index"]))
    return out


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]


@dataclass
class CompletionResult:
    content: str
    raw: dict


def complete(messages: list[dict], model_name: str = config.EXTRACTION_MODEL_NAME,
             max_tokens: int = 1500, temperature: float = 0.0) -> CompletionResult:
    """Chat completion via the orchestration deployment."""
    url = _deployment_url(config.ORCHESTRATION_DEPLOYMENT_ID) + "/completion"
    payload = {
        "orchestration_config": {
            "module_configurations": {
                "llm_module_config": {
                    "model_name": model_name,
                    "model_params": {"max_tokens": max_tokens, "temperature": temperature},
                },
                "templating_module_config": {"template": messages},
            }
        }
    }
    resp = requests.post(url, headers=_headers(), json=payload, timeout=60)
    resp.raise_for_status()
    body = resp.json()
    content = body["orchestration_result"]["choices"][0]["message"]["content"]
    return CompletionResult(content=content, raw=body)

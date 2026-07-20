"""Make one small non-persisted DeepSeek JSON-output request without printing content."""

from __future__ import annotations

import asyncio

from app.ai.evidence import PROMPT_VERSION, DeepSeekEvidenceProvider
from app.config import Settings
from app.domain.evidence import EvidenceExtractionRequest


async def run() -> int:
    settings = Settings()
    if settings.deepseek_api_key is None:
        print("DeepSeek API key is not configured")
        return 2
    provider = DeepSeekEvidenceProvider(
        credential=settings.deepseek_api_key.get_secret_value(),
        model=settings.deepseek_model,
        base_url=str(settings.deepseek_base_url),
        max_input_characters=2_000,
        max_output_tokens=min(settings.deepseek_max_output_tokens, 600),
        timeout_seconds=settings.deepseek_timeout_seconds,
        proxy_url=(str(settings.collector_proxy_url) if settings.collector_proxy_url else None),
    )
    result = await provider.extract(
        EvidenceExtractionRequest(
            document_id="connection-check",
            title="Semiconductor HBM shipment update",
            summary="A manufacturer reported higher HBM shipments.",
            normalized_body=(
                "A semiconductor manufacturer reported that HBM shipments increased "
                "during the quarter. The statement did not provide an industry forecast."
            ),
            language="en",
            topic_ids=("semiconductor", "semiconductor.memory"),
            source_kind="MEDIA",
            prompt_version=PROMPT_VERSION,
        )
    )
    print("DeepSeek connection and schema validation succeeded")
    print(f"model: {result.model_version}")
    print(f"input tokens: {result.input_tokens}")
    print(f"output tokens: {result.output_tokens}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))

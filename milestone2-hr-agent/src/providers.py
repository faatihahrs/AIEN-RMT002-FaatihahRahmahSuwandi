from __future__ import annotations

import json
import re
from typing import Any, Protocol
from uuid import uuid4

from openai import OpenAI

from .contracts import ModelToolCall, ModelTurn


class ModelProvider(Protocol):
    def embed(
        self,
        texts: list[str],
    ) -> list[list[float]]: ...

    def respond(
        self,
        *,
        input_data: str | list[Any],
        instructions: str,
        tools: list[dict[str, Any]],
        safety_identifier: str,
        max_output_tokens: int,
    ) -> ModelTurn: ...


class OpenAIProvider:
    """OpenAI Responses API and embedding provider."""

    def __init__(
        self,
        *,
        api_key: str,
        agent_model: str,
        embedding_model: str,
        embedding_dimensions: int,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY belum diatur")

        if not agent_model:
            raise ValueError("AGENT_MODEL belum diatur")

        if not embedding_model:
            raise ValueError("EMBEDDING_MODEL belum diatur")

        self.agent_model = agent_model
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions

        client_arguments: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_seconds,
            # Bounded SDK retry for transient provider failures.
            "max_retries": 2,
        }

        if base_url:
            client_arguments["base_url"] = base_url

        self.client = OpenAI(**client_arguments)

    def embed(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        if not texts:
            return []

        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError(
                "Semua input embedding harus berupa teks non-kosong"
            )

        response = self.client.embeddings.create(
            model=self.embedding_model,
            input=texts,
            dimensions=self.embedding_dimensions,
        )

        ordered = sorted(
            response.data,
            key=lambda item: item.index,
        )
        embeddings = [item.embedding for item in ordered]

        if len(embeddings) != len(texts):
            raise RuntimeError(
                "Jumlah embedding tidak sesuai jumlah input"
            )

        for embedding in embeddings:
            if len(embedding) != self.embedding_dimensions:
                raise RuntimeError(
                    "Dimensi embedding tidak sesuai konfigurasi: "
                    f"diharapkan {self.embedding_dimensions}, "
                    f"diterima {len(embedding)}"
                )

        return embeddings

    def respond(
        self,
        *,
        input_data: str | list[Any],
        instructions: str,
        tools: list[dict[str, Any]],
        safety_identifier: str,
        max_output_tokens: int,
    ) -> ModelTurn:
        response = self.client.responses.create(
            model=self.agent_model,
            input=input_data,
            instructions=instructions,
            tools=tools,
            safety_identifier=safety_identifier,
            max_output_tokens=max_output_tokens,
            store=False,
        )

        tool_calls: list[ModelToolCall] = []
        output_items: list[Any] = []

        for item in response.output:
            if hasattr(item, "model_dump"):
                serialized = item.model_dump(
                    mode="json",
                    exclude_none=True,
                )
            else:
                serialized = item

            output_items.append(serialized)

            if getattr(item, "type", None) != "function_call":
                continue

            raw_arguments = getattr(item, "arguments", "{}")

            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "Model menghasilkan argumen tool yang bukan JSON"
                ) from exc

            if not isinstance(arguments, dict):
                raise RuntimeError(
                    "Argumen tool harus berupa JSON object"
                )

            call_id = (
                getattr(item, "call_id", None)
                or getattr(item, "id", None)
            )

            if not call_id:
                raise RuntimeError(
                    "Model tool call tidak memiliki call_id"
                )

            tool_calls.append(
                ModelToolCall(
                    call_id=call_id,
                    name=getattr(item, "name"),
                    arguments=arguments,
                )
            )

        usage = getattr(response, "usage", None)

        input_tokens = (
            int(getattr(usage, "input_tokens", 0) or 0)
            if usage
            else 0
        )
        output_tokens = (
            int(getattr(usage, "output_tokens", 0) or 0)
            if usage
            else 0
        )

        return ModelTurn(
            response_id=response.id,
            text=response.output_text or "",
            tool_calls=tool_calls,
            output_items=output_items,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


class DeterministicProvider:
    """Network-free fake for deterministic tests."""

    embedding_dimensions = 8

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    def embed(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        import hashlib
        import math

        embeddings: list[list[float]] = []

        for text in texts:
            vector = [0.0] * self.embedding_dimensions

            for token in self._tokenize(text):
                digest = hashlib.sha256(
                    token.encode("utf-8")
                ).digest()
                index = digest[0] % self.embedding_dimensions
                sign = 1.0 if digest[1] % 2 == 0 else -1.0
                vector[index] += sign

            norm = math.sqrt(
                sum(value * value for value in vector)
            )

            if norm > 0:
                vector = [
                    value / norm
                    for value in vector
                ]

            embeddings.append(vector)

        return embeddings

    @staticmethod
    def _turn(
        *,
        text: str = "",
        tool_calls: list[ModelToolCall] | None = None,
    ) -> ModelTurn:
        return ModelTurn(
            response_id=f"fake-{uuid4().hex}",
            text=text,
            tool_calls=tool_calls or [],
            input_tokens=20,
            output_tokens=max(5, len(text.split())),
        )

    def respond(
        self,
        *,
        input_data: str | list[Any],
        instructions: str,
        tools: list[dict[str, Any]],
        safety_identifier: str,
        max_output_tokens: int,
    ) -> ModelTurn:
        del instructions, tools, safety_identifier, max_output_tokens

        if isinstance(input_data, str):
            message = input_data.strip()
            lowered = message.lower()

            if "saldo" in lowered:
                return self._turn(
                    tool_calls=[
                        ModelToolCall(
                            call_id=f"call-{uuid4().hex}",
                            name="get_my_leave_balance",
                            arguments={},
                        )
                    ]
                )

            return self._turn(
                tool_calls=[
                    ModelToolCall(
                        call_id=f"call-{uuid4().hex}",
                        name="search_hr_policy",
                        arguments={
                            "query": message,
                            "top_k": 3,
                        },
                    )
                ]
            )

        outputs = [
            item
            for item in input_data
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
        ]

        if not outputs:
            return self._turn(
                text=(
                    "Maaf, saya belum memiliki bukti yang cukup "
                    "untuk menjawab."
                )
            )

        try:
            payload = json.loads(outputs[-1]["output"])
            tool_name = payload["tool_name"]
            result = payload["result"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return self._turn(
                text=(
                    "Maaf, hasil tool tidak dapat diverifikasi."
                )
            )

        if tool_name == "search_hr_policy":
            results = result.get("results", [])

            if not results:
                return self._turn(
                    text=(
                        "Maaf, saya tidak menemukan kebijakan HR "
                        "yang cukup relevan."
                    )
                )

            first = results[0]
            answer = (
                f"{first['content']} "
                f"[{first['document_id']}]"
            )
            return self._turn(text=answer)

        if tool_name == "get_my_leave_balance":
            balances = result.get("balances", [])

            if not balances:
                return self._turn(
                    text="Saldo cuti tidak ditemukan."
                )

            labels = {
                "annual": "cuti tahunan",
                "sick": "cuti sakit",
                "parental": "cuti orang tua",
            }
            parts = [
                (
                    f"{labels.get(item['leave_type'], item['leave_type'])}: "
                    f"{item['available_days']} hari"
                )
                for item in balances
            ]

            return self._turn(
                text="Saldo Anda adalah " + ", ".join(parts) + "."
            )

        if tool_name == "prepare_leave_request":
            proposal = result.get("proposal", {})
            return self._turn(
                text=(
                    "Proposal cuti telah disiapkan untuk "
                    f"{proposal.get('business_days', 0)} hari kerja. "
                    "Belum ada pengajuan yang dibuat; silakan "
                    "konfirmasi proposal terlebih dahulu."
                )
            )

        if tool_name == "create_hr_ticket":
            return self._turn(
                text=(
                    "Tiket HR berhasil dibuat dengan ID "
                    f"{result.get('ticket_id', '-')}. "
                    "Tiket akan ditinjau manusia."
                )
            )

        return self._turn(
            text="Maaf, hasil tool tidak dikenali."
        )
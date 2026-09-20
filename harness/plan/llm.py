"""Talking to the model, and being able to prove afterwards what was said.

Real calls go to the Claude API through the official SDK, using structured
outputs so a response is a validated Pydantic object rather than prose somebody
has to parse. Every call is recorded: the system prompt, the user content, the
schema asked for, the parsed result, and the token usage.

The recording is not a test fixture bolted on afterwards. It serves three jobs
at once, which is why it is in the main path rather than in `tests/`.

**The demo has to be reproducible.** A graded run that produces different text
every time cannot be checked against its own audit log. `replay` mode makes the
scenarios deterministic without pretending the model was never involved.

**Tests have to run without a network or a bill.** Everything below the planner
is deterministic already; recording makes the planner testable on the same
terms.

**The audit has to show what the model actually saw.** "What the agent
concluded" is not reconstructable without the context window that produced it.
The cassette is that record, and `runs/<id>/03-prompt.json` is the readable
copy.

Modes, set by `SILO_LLM_MODE`:

    auto     replay a recorded call when one exists, otherwise call the API
             and record it. The default, and what `make demo` uses.
    live     always call the API, never read the cassette
    record   always call the API and overwrite the cassette
    replay   never call the API; a missing cassette is an error

Cassettes are keyed on the situation rather than on the exact bytes of the
prompt, because the prompt contains generated identifiers that differ between
runs. The full prompt is stored in the cassette regardless, so a reader can see
exactly what was sent even though the key does not depend on all of it.
"""
from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 8000


@dataclass
class ModelCall:
    """One exchange with the model, in full."""

    key: str
    purpose: str
    model: str
    system: str
    user: str
    schema_name: str
    output: dict
    usage: dict = field(default_factory=dict)
    replayed: bool = False

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "purpose": self.purpose,
            "model": self.model,
            "schema": self.schema_name,
            "system": self.system,
            "user": self.user,
            "output": self.output,
            "usage": self.usage,
            "replayed": self.replayed,
        }


class ModelUnavailable(RuntimeError):
    """No cassette in replay mode, or no credentials in live mode."""


def cassette_key(purpose: str, situation: str, schema_name: str) -> str:
    digest = hashlib.sha256(f"{purpose}|{situation}|{schema_name}".encode()).hexdigest()
    return f"{purpose}.{digest[:16]}"


class CassetteStore:
    """Recorded calls, one JSON file per key, readable and diffable."""

    def __init__(self, directory: str | Path) -> None:
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=True)

    def _file(self, key: str) -> Path:
        return self.path / f"{key}.json"

    def has(self, key: str) -> bool:
        return self._file(key).exists()

    def read(self, key: str) -> ModelCall:
        data = json.loads(self._file(key).read_text(encoding="utf-8"))
        return ModelCall(
            key=data["key"], purpose=data["purpose"], model=data["model"],
            system=data["system"], user=data["user"],
            schema_name=data["schema"], output=data["output"],
            usage=data.get("usage", {}), replayed=True,
        )

    def write(self, call: ModelCall) -> None:
        self._file(call.key).write_text(
            json.dumps(call.as_dict() | {"replayed": False}, indent=2) + "\n",
            encoding="utf-8",
        )


class LLMClient(ABC):
    """The one thing the harness asks of a model: a validated object."""

    @abstractmethod
    def structured(
        self,
        *,
        purpose: str,
        situation: str,
        system: str,
        user: str,
        output_model: type[T],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> tuple[T, ModelCall]:
        """Return a validated instance of `output_model`, and the call record."""


class AnthropicClient(LLMClient):
    """Live calls against the Claude API."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        try:
            import anthropic
        except ImportError as error:  # pragma: no cover
            raise ModelUnavailable(
                "the anthropic package is not installed; "
                "pip install -r requirements.txt"
            ) from error
        self._anthropic = anthropic
        self.model = model
        self._client = anthropic.Anthropic()

    def structured(
        self,
        *,
        purpose: str,
        situation: str,
        system: str,
        user: str,
        output_model: type[T],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> tuple[T, ModelCall]:
        # The system prompt is the stable prefix across every call of a given
        # purpose (the tool catalogue, the rules, the output contract), and the
        # user content is the part that changes per run. Caching the system
        # block is therefore free and correct.
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": user}],
            output_format=output_model,
            thinking={"type": "adaptive"},
        )
        if response.stop_reason == "refusal":
            raise ModelUnavailable(
                f"the model declined this request: {response.stop_details}"
            )

        parsed = response.parsed_output
        call = ModelCall(
            key=cassette_key(purpose, situation, output_model.__name__),
            purpose=purpose,
            model=self.model,
            system=system,
            user=user,
            schema_name=output_model.__name__,
            output=parsed.model_dump(mode="json"),
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": getattr(
                    response.usage, "cache_read_input_tokens", 0
                ),
            },
        )
        return parsed, call


class CassetteClient(LLMClient):
    """Replay, record, or both, wrapping a live client when it needs one."""

    def __init__(
        self,
        store: CassetteStore,
        *,
        mode: str = "auto",
        live: LLMClient | None = None,
    ) -> None:
        if mode not in ("auto", "live", "record", "replay"):
            raise ValueError(f"unknown SILO_LLM_MODE: {mode!r}")
        self.store = store
        self.mode = mode
        self._live = live

    def _live_client(self) -> LLMClient:
        if self._live is None:
            self._live = AnthropicClient()
        return self._live

    def structured(
        self,
        *,
        purpose: str,
        situation: str,
        system: str,
        user: str,
        output_model: type[T],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> tuple[T, ModelCall]:
        key = cassette_key(purpose, situation, output_model.__name__)

        if self.mode in ("auto", "replay") and self.store.has(key):
            call = self.store.read(key)
            # Validated on the way out as well as on the way in. A cassette
            # edited by hand, or left behind by an older schema, should fail
            # here rather than flow through as a plausible-looking object.
            return output_model.model_validate(call.output), call

        if self.mode == "replay":
            raise ModelUnavailable(
                f"no cassette for {key} and SILO_LLM_MODE=replay. "
                f"Run once with SILO_LLM_MODE=record to create it."
            )

        parsed, call = self._live_client().structured(
            purpose=purpose, situation=situation, system=system, user=user,
            output_model=output_model, max_tokens=max_tokens,
        )
        self.store.write(call)
        return parsed, call


def load_env(path: str | Path = ".env") -> None:
    """Read a local `.env` into the environment, without a dependency.

    Existing variables win, so an exported key beats the file and CI never
    picks up somebody's laptop credentials. The file is gitignored; `.env.example`
    is the committed copy that documents what belongs in it.
    """
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def build_client(
    cassette_dir: str | Path = "cassettes", *, mode: str | None = None
) -> LLMClient:
    """The client the CLI and the tests both use."""
    load_env(Path(__file__).resolve().parents[2] / ".env")
    return CassetteClient(
        CassetteStore(cassette_dir), mode=mode or os.environ.get("SILO_LLM_MODE", "auto")
    )

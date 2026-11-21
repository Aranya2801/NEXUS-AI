"""Routing configuration (JSON, inline or `@/path/file.json`). Secrets are NEVER in the file: models name an environment variable."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.config import Settings
from app.core.exceptions import ConfigurationError


class ModelCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["mock", "openai", "anthropic"]
    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    price_in_per_mtok: float = Field(default=0.0, ge=0)       # USD per 1M prompt tokens
    price_out_per_mtok: float = Field(default=0.0, ge=0)      # USD per 1M completion tokens
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)


class BudgetCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daily_usd_per_org: float | None = Field(default=None, gt=0)
    degrade_at: float = Field(default=0.8, gt=0, le=1)         # fraction of the budget after which cheap routing is forced
    degrade_chain: list[str] = []                              # model names (cheapest first); empty = keep routing but still enforce the cap


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: dict[str, ModelCfg]
    default_chain: list[str]
    task_chains: dict[str, list[str]] = {}
    budget: BudgetCfg = BudgetCfg()
    breaker_threshold: int = Field(default=3, ge=1)
    breaker_window_seconds: float = Field(default=60.0, gt=0)
    breaker_cooldown_seconds: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _known_models(self):
        chains = [self.default_chain, self.budget.degrade_chain, *self.task_chains.values()]
        for ch in chains:
            for m in ch:
                if m not in self.models:
                    raise ValueError(f"chain references unknown model '{m}'")
        if not self.default_chain:
            raise ValueError("default_chain must not be empty")
        return self


def load_routing_config(s: Settings) -> RoutingConfig | None:
    raw = s.llm_routing_config
    if not raw:
        return None
    try:
        text = Path(raw[1:]).read_text() if raw.startswith("@") else raw
        cfg = RoutingConfig(**json.loads(text))
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise ConfigurationError(f"Invalid NEXUS_LLM_ROUTING_CONFIG: {exc}") from exc
    for name, m in cfg.models.items():
        if m.api_key_env and not os.environ.get(m.api_key_env):
            raise ConfigurationError(f"Model '{name}' needs environment variable {m.api_key_env}")
    return cfg

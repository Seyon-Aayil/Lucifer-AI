"""
master.orchestrator.schemas
============================
Pydantic schemas for structured LLM outputs in the orchestrator.

These replace brittle dict/string parsing of model output with validated
models. The JSON schema (`model_json_schema()`) can also be handed to a local
model (e.g. Ollama's `format` field) to constrain generation at the source.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from master.agents.base.agent import RiskTier


class IntentClassification(BaseModel):
    """Validated output of the intent classifier (intent + agent + risk tier)."""

    # Extra keys from the model are ignored rather than raising.
    model_config = ConfigDict(extra="ignore")

    intent: str = "chat"
    agent_id: str = "personal-agent"
    risk_tier: RiskTier = RiskTier.LOW

    @field_validator("risk_tier", mode="before")
    @classmethod
    def _coerce_risk(cls, v: object) -> RiskTier:
        """Map an unknown/invalid risk value to LOW instead of failing validation."""
        if isinstance(v, RiskTier):
            return v
        try:
            return RiskTier(str(v).lower())
        except ValueError:
            return RiskTier.LOW

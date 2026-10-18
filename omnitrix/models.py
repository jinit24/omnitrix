from typing import Literal
from .research_models import ResearchPlan
from pydantic import BaseModel, Field, ConfigDict


class Handoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal['completed', 'blocked', 'failed']
    summary: str = Field(max_length=6000)
    evidence: list[str] = Field(max_length=20)
    next_steps: list[str] = Field(max_length=10)


class Decision(BaseModel):
    action: Literal['delegate', 'follow_up', 'complete', 'pause', 'discover', 'research', 'retry_research', 'implement', 'audit']
    rationale: str = Field(max_length=3000)
    instructions: str = Field(max_length=6000)
    task_id: str | None
    priority: int = Field(ge=1, le=5)

    research_plan: ResearchPlan | None = None
    research_id: str | None = None
    implementation_id: str | None = None
    pause_reason: Literal['research', 'authorization', 'external_input', 'budget', 'low_value', 'reconciliation'] | None = None


class ChatReply(BaseModel):
    response: str
    proposal_kind: Literal['none', 'policy', 'requirements', 'steer', 'cancel', 'pause']
    proposal_text: str
    affected_tasks: list[str]
    add_requirements: list[str]

from typing import Annotated, Literal
from .research_models import ResearchPlan, FocusedResearchPlan, DiscoveryResearchPlan
from pydantic import BaseModel, Field, ConfigDict, field_validator


class Handoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal['completed', 'blocked', 'failed']
    summary: str = Field(max_length=6000)
    evidence: list[str] = Field(max_length=20)
    next_steps: list[str] = Field(max_length=10)


class ScheduledDiscovery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stage: Literal['discovery']
    plan: DiscoveryResearchPlan
    priority: int = Field(default=3, ge=1, le=5)
    instructions: str = Field(default='', max_length=6000)

    @field_validator('plan', mode='before')
    @classmethod
    def normalize_legacy_plan(cls, value):
        return value.model_dump() if isinstance(value, ResearchPlan) else value


class ScheduledFocusedResearch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stage: Literal['research']
    plan: FocusedResearchPlan
    priority: int = Field(default=3, ge=1, le=5)
    instructions: str = Field(default='', max_length=6000)

    @field_validator('plan', mode='before')
    @classmethod
    def normalize_legacy_plan(cls, value):
        return value.model_dump() if isinstance(value, ResearchPlan) else value


ScheduledResearch = Annotated[ScheduledDiscovery | ScheduledFocusedResearch,
                              Field(discriminator='stage')]


class Decision(BaseModel):
    action: Literal['delegate', 'follow_up', 'complete', 'pause', 'discover', 'research', 'retry_research', 'reevaluate', 'implement', 'audit', 'schedule_research']
    rationale: str = Field(max_length=3000)
    instructions: str = Field(max_length=6000)
    task_id: str | None
    priority: int = Field(ge=1, le=5)

    research_plan: FocusedResearchPlan | DiscoveryResearchPlan | None = None
    research_batch: list[ScheduledResearch] = Field(default_factory=list, max_length=6)

    @field_validator('research_plan', mode='before')
    @classmethod
    def normalize_legacy_plan(cls, value):
        return value.model_dump() if isinstance(value,ResearchPlan) else value
    research_id: str | None = None
    implementation_id: str | None = None


class ChatReply(BaseModel):
    response: str
    proposal_kind: Literal['none', 'policy', 'requirements', 'steer', 'cancel', 'pause']
    proposal_text: str
    affected_tasks: list[str]
    add_requirements: list[str]

"""Typed contracts for bounded research, rather than an autonomous task graph."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ResearchBudget(Contract):
    tool_calls: int = Field(default=8, ge=1, le=24)
    seconds: int = Field(default=60, ge=1, le=180)
    attempts: int = Field(default=2, ge=1, le=3)


class ResearchPlan(Contract):
    scope: list[str] = Field(min_length=1, max_length=20)
    question: str = Field(min_length=10, max_length=2000)
    hypothesis: str | None = Field(default=None, max_length=2000)
    falsification: str | None = Field(default=None, max_length=2000)
    opportunity_id: str | None = None
    budget: ResearchBudget = Field(default_factory=ResearchBudget)

    @model_validator(mode='after')
    def narrow_hypothesis(self):
        if self.hypothesis is not None:
            if not self.hypothesis.strip() or not (self.falsification or '').strip():
                raise ValueError('Research requires a hypothesis and an explicit falsification test')
            if len(self.scope) > 8:
                raise ValueError('Focused research is limited to eight explicit sources')
        return self


class MapEntry(Contract):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal['service', 'dependency', 'job', 'repository', 'observability', 'document']
    description: str = Field(max_length=1500)
    depends_on: list[str] = Field(max_length=20)
    evidence_ids: list[str] = Field(max_length=10)
    assumption: bool


class Opportunity(Contract):
    title: str = Field(min_length=1, max_length=300)
    hypothesis: str = Field(min_length=10, max_length=2000)
    falsification: str = Field(min_length=10, max_length=2000)
    scope: list[str] = Field(min_length=1, max_length=8)
    impact: int = Field(ge=1, le=5)
    confidence: int = Field(ge=0, le=5)
    risk: int = Field(ge=1, le=5)
    effort: int = Field(ge=1, le=5)
    evidence_ids: list[str] = Field(max_length=10)
    rationale: str = Field(max_length=2000)


class DiscoveryReport(Contract):
    system_map: list[MapEntry] = Field(max_length=40)
    opportunities: list[Opportunity] = Field(max_length=12)
    gaps: list[str] = Field(max_length=20)


class Finding(Contract):
    statement: str = Field(min_length=1, max_length=3000)
    classification: Literal['claim', 'assumption']
    evidence_ids: list[str] = Field(max_length=10)
    category: Literal['mechanism', 'measurement', 'evidence_gap', 'observation'] = 'observation'
    workload: str | None = Field(default=None, max_length=1500)
    falsification: str | None = Field(default=None, max_length=1500)

    @model_validator(mode='after')
    def mechanism_contract(self):
        if self.category == 'mechanism' and (not (self.workload or '').strip() or not (self.falsification or '').strip()):
            raise ValueError('Mechanism findings require a workload and a falsification condition')
        return self


class ResearchReport(Contract):
    findings: list[Finding] = Field(max_length=15)
    supporting_evidence: list[str] = Field(max_length=20)
    contradictory_evidence: list[str] = Field(max_length=20)
    uncertainties: list[str] = Field(max_length=15)
    recommendation: str = Field(min_length=1, max_length=2000)
    blocked: bool


class Evaluation(Contract):
    verdict: Literal['supported', 'insufficient', 'falsified', 'blocked']
    evidence_ids: list[str] = Field(max_length=20)
    reason: str = Field(min_length=1, max_length=3000)
    feedback: str = Field(min_length=1, max_length=3000)

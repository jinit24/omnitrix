"""Typed contracts for bounded research, rather than an autonomous task graph."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ResearchContext(Contract):
    require_checkpoint: bool = False
    require_evidence_plan: bool = False
    brief: str = Field(min_length=1, max_length=20000)
    rubric: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def nonempty(self):
        if not self.brief.strip() or any(not item.strip() or len(item) > 2000 for item in self.rubric):
            raise ValueError('Brief and rubric items must be nonempty; rubric items are limited to 2000 characters')
        if len(set(self.rubric)) != len(self.rubric):
            raise ValueError('Rubric items must be unique')
        return self


class ResearchBudget(Contract):
    tool_calls: int = Field(default=8, ge=1, le=24)
    seconds: int = Field(default=60, ge=1, le=180)
    attempts: int = Field(default=2, ge=1, le=3)


EvidenceKind = Literal['implementation', 'caller', 'callee', 'contract', 'test', 'final_outcome']


class EvidenceObligation(Contract):
    kind: EvidenceKind
    locator: str = Field(min_length=1, max_length=512)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    status: Literal['required', 'captured', 'dependency'] = 'required'
    required: bool = True
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def ordered_range(self):
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError('Evidence obligation end_line must follow start_line')
        return self


class UnresolvedDependency(Contract):
    kind: EvidenceKind = 'callee'
    symbol: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=1500)
    locator: str = Field(default='', max_length=512)
    required: bool = True


class DependencySource(Contract):
    locator: str = Field(min_length=1, max_length=512)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(ge=1)


class ResearchPlan(Contract):
    review_budget_usd: float = Field(default=1.5, gt=0, le=25, allow_inf_nan=False)
    primary_area: str = Field(default='', max_length=512)
    evidence_obligations: list[EvidenceObligation] = Field(default_factory=list, max_length=24)
    unresolved_dependencies: list[UnresolvedDependency] = Field(default_factory=list, max_length=16)
    allow_scope_expansion: bool = False
    dependency_roots: list[str] = Field(default_factory=list, max_length=8)
    scope: list[str] = Field(min_length=1, max_length=20)
    question: str = Field(min_length=10, max_length=2000)
    hypothesis: str | None = Field(default=None, max_length=2000)
    falsification: str | None = Field(default=None, max_length=2000)
    decision: str = Field(default='', max_length=2000)
    expected_value: str = Field(default='', max_length=2000)
    stop_condition: str = Field(default='', max_length=2000)
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
    actionable: bool = True
    scope_gaps: list[str] = Field(default_factory=list, max_length=20)
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


class ResearchReport(Contract):
    stop_reason: Literal['resolved', 'incomplete_scope', 'access_unavailable', 'tool_budget', 'time_budget', 'low_value'] | None = None
    findings: list[Finding] = Field(max_length=15)
    supporting_evidence: list[str] = Field(max_length=20)
    contradictory_evidence: list[str] = Field(max_length=20)
    uncertainties: list[str] = Field(max_length=15)
    recommendation: str = Field(min_length=1, max_length=2000)
    blocked: bool


class RubricAssessment(Contract):
    criterion: str = Field(min_length=1, max_length=2000)
    verdict: Literal['met', 'unmet', 'not_applicable']
    reason: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[str] = Field(max_length=20)


class Evaluation(Contract):
    evidential_verdict: Literal['supported', 'insufficient', 'falsified', 'blocked'] | None = None
    workflow_issues: list[str] = Field(default_factory=list, max_length=20)
    rubric_assessments: list[RubricAssessment] = Field(default_factory=list, max_length=20)
    verdict: Literal['supported', 'insufficient', 'falsified', 'blocked']
    evidence_ids: list[str] = Field(max_length=20)
    reason: str = Field(min_length=1, max_length=3000)
    feedback: str = Field(min_length=1, max_length=3000)


class FocusedResearchPlan(ResearchPlan):
    """Expose focused scope limits in the model's structured-output schema."""
    scope: list[str] = Field(min_length=1, max_length=8)
    hypothesis: str = Field(min_length=1, max_length=2000)
    falsification: str = Field(min_length=1, max_length=2000)


class DiscoveryResearchPlan(ResearchPlan):
    hypothesis: None = None
    falsification: None = None

"""Deterministic specialist doubles; no claims of live model intelligence."""
from omnitrix.research_models import DiscoveryReport,MapEntry,Opportunity,ResearchReport,Finding,Evaluation


class FixtureAgents:
    def __init__(self,misleading=False,verdict='supported'):
        self.misleading,self.verdict=misleading,verdict
        self.evaluated_content=''

    async def run(self,role,context,tools):
        if role=='evaluation':
            self.evaluated_content='\n'.join(e['excerpt'] for e in context['evidence'])
            evidence=context['report']['supporting_evidence']
            return Evaluation(verdict=self.verdict,evidence_ids=evidence,
                reason='Read captured system registry independently',feedback='Inspect polling measurement and gather contrary evidence',
                test_kind='counterexample' if self.verdict=='falsified' else 'direct',test_evidence_ids=evidence)
        source=tools.read(context['plan']['scope'][0])
        source_id='invented-evidence' if self.misleading else source['id']
        if role=='discovery':
            def opportunity(title,impact,confidence,risk,effort):
                return Opportunity(title=title,hypothesis='Queue polling interval is 30 seconds',
                    falsification='Registry specifies a different interval',scope=context['plan']['scope'],impact=impact,
                    confidence=confidence,risk=risk,effort=effort,evidence_ids=[source_id],rationale='Registry suggests measurable latency opportunity')
            return DiscoveryReport(system_map=[MapEntry(name='queue',kind='service',description='Polling worker',
                depends_on=['database'],evidence_ids=[source_id],assumption=False)],
                opportunities=[opportunity('Polling latency',5,4,1,1),opportunity('Database tuning',2,1,4,5)],gaps=['Need production latency measurements'])
        return ResearchReport(findings=[Finding(statement='Queue polling interval is 30 seconds',classification='claim',evidence_ids=[source_id])],
            supporting_evidence=[source_id],contradictory_evidence=[source_id] if self.verdict=='falsified' else [],uncertainties=['Production measurement unavailable'],
            recommendation='Return to manager for prioritization',blocked=False)

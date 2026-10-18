"""Usage is measured independently of engineering progress."""
from agents import RunHooks


class UsageRecorder(RunHooks):
    """Save each completed provider response, including runs that later exhaust turns."""
    def __init__(self, store, goal_id, role):
        self.store, self.goal_id, self.role = store, goal_id, role

    async def on_llm_end(self, context, agent, response):
        if self.store is None:
            return
        from .control import Control
        usage = response.usage
        Control(self.store).event(self.goal_id, 'usage', {'role': self.role, 'requests': usage.requests,
            'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens})


def record_usage(store, goal_id, result, role):
    usage = getattr(getattr(result, 'context_wrapper', None), 'usage', None)
    if usage is None:
        return
    from .control import Control
    Control(store).event(goal_id, 'usage', {'role': role, 'requests': usage.requests,
        'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens})

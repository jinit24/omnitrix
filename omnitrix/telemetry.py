"""Usage is measured independently of engineering progress."""
def record_usage(store, goal_id, result, role):
    usage = getattr(getattr(result, 'context_wrapper', None), 'usage', None)
    if usage is None:
        return
    from .control import Control
    Control(store).event(goal_id, 'usage', {'role': role, 'requests': usage.requests,
        'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens})

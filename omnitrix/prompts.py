"""Shared quality policy and explicit role prompts for every model entry point."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ROLE_FILES = {
    'campaign': ('CAMPAIGN', 'BRAIN', 'KNOWLEDGE'),
    'manager': ('MANAGER', 'BRAIN', 'KNOWLEDGE'),
    'chat': ('CHAT', 'BRAIN', 'KNOWLEDGE'),
    'discovery': ('DISCOVERY',),
    'research': ('RESEARCH',),
    'investigator': ('RESEARCH', 'INVESTIGATOR', 'KNOWLEDGE'),
    'goal_review': ('GOAL_REVIEW',),
    'evaluation': ('EVALUATION',),
    'worker': ('WORKER',),
    'implementation': ('IMPLEMENTATION',),
}


def load_instructions(role: str, policy: str = '') -> str:
    """Compose at invocation time so new and resumed turns see current prompts.

    Task text and source material remain separate from these role instructions.
    Unknown roles and missing files fail instead of silently skipping quality rules.
    """
    files = ('QUALITY_GATES', *ROLE_FILES[role])
    packaged = Path(__file__).with_name('instructions')
    directory = packaged if packaged.is_dir() else ROOT / 'instructions'
    parts = [(directory / f'{name}.md').read_text() for name in files]
    if any(not part.strip() for part in parts):
        raise ValueError(f'Empty instruction file for role {role}')
    if policy.strip():
        parts.append('Current user-approved durable policy:\n' + policy)
    return '\n\n'.join(parts)

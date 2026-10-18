"""Use environment API authentication without persisting or copying credentials."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def api_provider_config():
    if not os.getenv('OPENAI_API_KEY'):
        return {}
    base_url = os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
    parsed = urlsplit(base_url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('OPENAI_BASE_URL must be an HTTPS API endpoint without credentials or query parameters')
    return {
        'model_provider': 'omnitrix_api',
        'model': os.getenv('OMNITRIX_CODEX_MODEL', 'gpt-5.3-codex'),
        'model_reasoning_effort': os.getenv('OMNITRIX_CODEX_REASONING_EFFORT', 'medium'),
        'model_providers': {'omnitrix_api': {
            'name': 'OpenAI API', 'base_url': base_url, 'env_key': 'OPENAI_API_KEY',
            'wire_api': 'responses', 'requires_openai_auth': False,
        }},
    }


def codex_environment():
    env = os.environ.copy()
    if os.getenv('OPENAI_API_KEY'):
        home = Path(os.getenv('OMNITRIX_CODEX_HOME', str(ROOT / '.omnitrix/codex-api'))).resolve()
        home.mkdir(parents=True, exist_ok=True)
        env['CODEX_HOME'] = str(home)
    return env


def cli_config_args():
    # TOML supports JSON quoted strings. Expand nested provider fields into dotted keys.
    result = []
    def add(prefix, value):
        if isinstance(value, dict):
            for key, child in value.items():
                add(f'{prefix}.{key}' if prefix else key, child)
        else:
            result.extend(['-c', f'{prefix}={json.dumps(value)}'])
    add('', api_provider_config())
    return result

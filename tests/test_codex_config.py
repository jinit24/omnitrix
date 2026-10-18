import json
from omnitrix.codex_config import api_provider_config, cli_config_args, codex_environment


def test_api_key_is_referenced_by_name_and_never_written(monkeypatch,tmp_path):
    monkeypatch.setenv('OPENAI_API_KEY','test-secret-not-a-real-key')
    monkeypatch.setenv('OMNITRIX_CODEX_HOME',str(tmp_path/'codex'))
    monkeypatch.delenv('OPENAI_BASE_URL',raising=False)
    config=api_provider_config()
    assert config['model_providers']['omnitrix_api']['env_key']=='OPENAI_API_KEY'
    assert not config['model_providers']['omnitrix_api']['requires_openai_auth']
    assert 'test-secret-not-a-real-key' not in json.dumps(config)
    assert 'test-secret-not-a-real-key' not in ' '.join(cli_config_args())
    env=codex_environment()
    assert env['CODEX_HOME']==str(tmp_path/'codex')
    assert not list((tmp_path/'codex').iterdir())


def test_without_api_key_existing_codex_configuration_is_preserved(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    monkeypatch.setenv('CODEX_HOME','/existing/authenticated/home')
    assert api_provider_config()=={}
    assert cli_config_args()==[]
    assert codex_environment()['CODEX_HOME']=='/existing/authenticated/home'


def test_api_endpoint_rejects_embedded_credentials(monkeypatch):
    import pytest
    monkeypatch.setenv('OPENAI_API_KEY','test-key')
    monkeypatch.setenv('OPENAI_BASE_URL','https://user:password@example.com/v1')
    with pytest.raises(ValueError,match='without credentials'):
        api_provider_config()

"""Offline checks for the direct Anthropic report backend."""
import os
import copy
from types import SimpleNamespace
from unittest.mock import patch

from agent.llm import AnthropicLLM
from agent.providers import get_llm, normalize_config


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        text = 'Part one' if len(self.calls) == 1 else 'Part two'
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=text)],
                               stop_reason='max_tokens' if len(self.calls) == 1 else 'end_turn',
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def test_continuation_and_no_temperature():
    client = SimpleNamespace(messages=FakeMessages())
    llm = AnthropicLLM({'anthropic_model': 'claude-test', 'report_max_tokens': 100}, client=client)
    assert llm.generate('sys', 'payload') == 'Part onePart two'
    assert len(client.messages.calls) == 2
    assert client.messages.calls[0] == {'model': 'claude-test', 'max_tokens': 100,
                                       'system': 'sys', 'messages': [{'role': 'user', 'content': 'payload'}]}
    assert client.messages.calls[1]['messages'][-1] == {
        'role': 'user', 'content': 'Continue the report exactly where you stopped. Do not repeat content.'}
    assert all('temperature' not in call for call in client.messages.calls)


def test_provider_and_missing_key():
    cfg = normalize_config({'input_mode': 'existing_bucket', 'bucket': '/tmp/bucket',
                            'llm_provider': 'anthropic', 'anthropic_model': 'claude-test'}, 'local')
    with patch.dict(os.environ, {}, clear=True):
        try:
            get_llm(cfg)
        except ValueError as exc:
            assert 'ANTHROPIC_API_KEY' in str(exc)
        else:
            raise AssertionError('Missing API key should fail')


if __name__ == '__main__':
    test_continuation_and_no_temperature()
    test_provider_and_missing_key()
    print('direct Anthropic provider checks passed')

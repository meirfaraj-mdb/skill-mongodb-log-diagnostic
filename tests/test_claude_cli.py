"""Offline tests for the local Claude Code CLI report provider."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent.llm import ClaudeCLILLM
from agent.providers import get_llm, normalize_config


def test_report_and_no_api_key():
    with tempfile.TemporaryDirectory() as tmp:
        cli = Path(tmp) / 'claude'
        args_file = Path(tmp) / 'args'
        prompt_file = Path(tmp) / 'prompt'
        api_file = Path(tmp) / 'api-present'
        cli.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_ARGS"\n'
                       'cat > "$TEST_PROMPT"\n'
                       '[ -n "${ANTHROPIC_API_KEY:-}" ] && touch "$TEST_API"\n'
                       'printf "# Local report\\n"\n')
        cli.chmod(0o755)
        cfg = normalize_config({'input_mode': 'existing_bucket', 'bucket': tmp,
                                'llm_provider': 'claude_cli', 'claude_cli_model': 'sonnet'}, 'local')
        with patch.dict(os.environ, {'PATH': tmp + os.pathsep + os.environ.get('PATH', ''),
                                     'ANTHROPIC_API_KEY': 'should-not-pass',
                                     'TEST_ARGS': str(args_file), 'TEST_PROMPT': str(prompt_file),
                                     'TEST_API': str(api_file)}):
            llm = get_llm(cfg)
            assert llm.generate('System rules', 'Extract data') == '# Local report'
        assert args_file.read_text().splitlines() == ['-p', '--output-format', 'text', '--tools', '', '--model', 'sonnet']
        assert 'System rules' in prompt_file.read_text()
        assert 'Extract data' in prompt_file.read_text()
        assert not api_file.exists()


def test_local_only():
    with patch.dict(os.environ, {'PATH': ''}):
        try:
            get_llm({'llm_provider': 'claude_cli', 'cloud': 'aws', 'storage_provider': 's3'})
        except ValueError as exc:
            assert 'local' in str(exc)
        else:
            raise AssertionError('CLI must not run in hosted environments')


def test_failure_does_not_disclose_stderr():
    with tempfile.TemporaryDirectory() as tmp:
        cli = Path(tmp) / 'claude'
        cli.write_text('#!/bin/sh\necho "sensitive log value" >&2\nexit 7\n')
        cli.chmod(0o755)
        with patch.dict(os.environ, {'PATH': tmp + os.pathsep + os.environ.get('PATH', '')}):
            try:
                ClaudeCLILLM({}).generate('rules', 'data')
            except RuntimeError as exc:
                assert 'exit 7' in str(exc)
                assert 'sensitive log value' not in str(exc)
            else:
                raise AssertionError('CLI failure should fail the report')


if __name__ == '__main__':
    test_report_and_no_api_key()
    test_local_only()
    test_failure_does_not_disclose_stderr()
    print('Claude CLI provider checks passed')

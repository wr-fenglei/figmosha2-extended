"""Keep independently configured bridge, CLI and plugin endpoints compatible."""
import importlib.util
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import pytest

import bridge

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('override', [None, 18788])
def test_bridge_listener_and_host_guard_use_selected_port(monkeypatch, override):
    argv = ['bridge.py'] + ([] if override is None else ['--port', str(override)])
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.setenv('FIGMOSHA_NO_UPDATE_CHECK', '1')
    monkeypatch.setattr(bridge, '_bind_hosts', lambda host, port: [host, '::1'])
    calls = []
    monkeypatch.setattr(bridge.web, 'run_app', lambda app, **kw: calls.append(kw))
    try:
        bridge.main()
        expected = 18787 if override is None else override
        assert calls[0]['port'] == expected
        assert calls[0]['host'] == ['127.0.0.1', '::1']
        assert bridge.ALLOWED_HOSTS == {
            f'localhost:{expected}', f'127.0.0.1:{expected}', f'[::1]:{expected}'
        }
    finally:
        for call in calls:
            call['loop'].close()


@pytest.mark.parametrize('env_port,arguments,expected', [
    (None, ['status'], 18787),
    ('18788', ['status'], 18788),
    ('18788', ['--port', '18789', 'status'], 18789),
])
def test_cli_default_environment_and_argument_precedence(monkeypatch, env_port, arguments, expected):
    monkeypatch.delenv('FIGMOSHA_PORT', raising=False)
    if env_port:
        monkeypatch.setenv('FIGMOSHA_PORT', env_port)
    spec = importlib.util.spec_from_file_location('figmosha_port_test', ROOT / 'figmosha.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.build_parser().parse_args(arguments).port == expected


def test_launchers_plugin_and_test_helpers_agree_on_default_port():
    ui = (ROOT / 'plugin/ui.html').read_text()
    endpoint = urlsplit(re.search(r'const SERVER = "([^"]+)";', ui).group(1))
    assert (endpoint.scheme, endpoint.hostname, endpoint.port, endpoint.path) == (
        'ws', 'localhost', 18787, '/plugin'
    )
    manifest = json.loads((ROOT / 'plugin/manifest.json').read_text())
    assert set(manifest['networkAccess']['allowedDomains']) == {
        'http://localhost:18787', 'ws://localhost:18787'
    }
    shell = (ROOT / 'start-bridge.sh').read_text()
    assert re.search(r'PORT="\$\{FIGMOSHA_PORT:-(\d+)\}"', shell).group(1) == '18787'
    windows = (ROOT / 'start-bridge.ps1').read_text()
    assert re.search(r'\[int\]\$Port = (\d+)', windows).group(1) == '18787'
    assert windows.isascii()
    mock = (ROOT / 'tests/fake_figma/run_plugin.js').read_text()
    assert re.search(r"opt\('--port', '(\d+)'\)", mock).group(1) == '18787'
    stress = (ROOT / 'tests/live_stress.py').read_text()
    assert 'BRIDGE = "http://127.0.0.1:18787"' in stress

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock
import pytest

@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location('dashboard_under_test', root/'dashboard.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.access_policy = __import__('access_control').AccessPolicy({'DASHBOARD_HOST':'0.0.0.0','DASHBOARD_TOKEN':'x'*32,'DASHBOARD_ALLOWED_HOSTS':'localhost'})
    module.app.testing = True
    return module

@pytest.mark.parametrize('route,method',[('/api/run','POST'),('/api/history/host','GET'),('/ssh','GET'),('/api/mirror/action','POST')])
def test_unauthenticated_requests_never_reach_side_effects(dashboard, monkeypatch, route, method):
    run = Mock(side_effect=AssertionError('No external work allowed'))
    monkeypatch.setattr(dashboard.subprocess,'run',run)
    response = dashboard.app.test_client().open(route, method=method, json={})
    assert response.status_code == 401
    assert response.headers['Cache-Control'] == 'no-store'
    run.assert_not_called()

def test_authorized_cross_site_shell_request_is_still_rejected(dashboard):
    response = dashboard.app.test_client().post('/api/run', json={}, headers={'Authorization':'Bearer '+'x'*32,'Origin':'https://attacker.invalid'})
    assert response.status_code == 403

def test_authenticated_history_read_still_works(dashboard):
    response = dashboard.app.test_client().get('/api/history/example',headers={'Authorization':'Bearer '+'x'*32,'Origin':'http://localhost'})
    assert response.status_code == 200
    assert response.json == {'history':[]}

@pytest.mark.parametrize("body", [[], {"hostname": [], "command": "status"}, {"hostname": "local", "command": {}}])
def test_malformed_shell_request_never_reaches_executor(dashboard, monkeypatch, body):
    run = Mock(side_effect=AssertionError("No external work allowed"))
    monkeypatch.setattr(dashboard.subprocess, "run", run)
    response = dashboard.app.test_client().post("/api/run", json=body, headers={"Authorization": "Bearer " + "x"*32})
    assert response.status_code == 400
    run.assert_not_called()

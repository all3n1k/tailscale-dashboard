import importlib.util
from pathlib import Path
import base64
import pytest

HERE = Path(__file__).resolve().parents[1]
MODULE_PATH = HERE / 'access_control.py'
if not MODULE_PATH.exists():
    MODULE_PATH = HERE / 'dimos_lite/access_control.py'
if not MODULE_PATH.exists():
    MODULE_PATH = HERE / 'brain/src/access_control.py'
spec = importlib.util.spec_from_file_location('policy_under_test', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
AccessPolicy = module.AccessPolicy

@pytest.mark.parametrize('headers', [
    {'host':'attacker.invalid'},
    {'host':'localhost:8080','origin':'https://attacker.invalid'},
    {'host':'localhost:8080','origin':'null'},
    {'host':'localhost:8080','origin':'http://[broken'},
    {'host':'localhost:8080','sec-fetch-site':'same-site'},
    {'host':'localhost:8080','sec-fetch-site':'cross-site'},
    {'host':'localhost:8080','origin':'http://localhost:9090'},
    {'host':'user:secret@localhost:8080'},
])
def test_rejects_rebinding_and_cross_origin(headers):
    assert AccessPolicy({}).check(headers)[0] == 403

def test_same_origin_local_controls_remain_available():
    assert AccessPolicy({}).check({'host':'localhost:8080','origin':'http://localhost:8080'}) is None

def test_remote_binding_requires_a_long_secret():
    policy = AccessPolicy({'DASHBOARD_HOST':'0.0.0.0'})
    with pytest.raises(ValueError):
        policy.validate_configuration()
    assert policy.check({'host':'localhost:8080'})[0] == 503

@pytest.mark.parametrize('auth', ['', 'Bearer wrong', 'Basic invalid', 'Basic bm9jb2xvbg=='])
def test_remote_access_fails_closed(auth):
    p = AccessPolicy({'DASHBOARD_HOST':'0.0.0.0','DASHBOARD_ALLOWED_HOSTS':'dashboard.example','DASHBOARD_TOKEN':'s'*32})
    assert p.check({'host':'dashboard.example','authorization':auth})[0] == 401

@pytest.mark.parametrize('auth',['Bearer '+'s'*32,'Basic '+base64.b64encode(('operator:'+('s'*32)).encode()).decode()])
def test_explicit_remote_authentication(auth):
    p = AccessPolicy({'DASHBOARD_HOST':'0.0.0.0','DASHBOARD_ALLOWED_HOSTS':'dashboard.example','DASHBOARD_TOKEN':'s'*32})
    assert p.check({'host':'dashboard.example','authorization':auth,'origin':'https://dashboard.example'}) is None

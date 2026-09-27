import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os
os.environ.pop('DAIL_REAL_PAYMENTS', None)
os.environ.pop('DATABASE_URL', None)
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
from fastapi.testclient import TestClient
from dail.api import app

def test_public_launch_and_payment_guard():
    c=TestClient(app)
    assert c.get('/').status_code == 200
    assert c.get('/launch').status_code == 200
    s=c.get('/payments/status').json()
    assert s['production_ready'] is False
    # checkout requires agent auth: no key -> 401
    r=c.post('/payments/checkout',json={'agent_id':'missing','usd_cents':100})
    assert r.status_code == 401
    # with a valid key, the rail-not-ready guard still fires (503)
    key=c.post('/agents',json={'id':'guard_agent','name':'guard'}).json()['api_key']
    r=c.post('/payments/checkout',json={'agent_id':'guard_agent','usd_cents':100},
             headers={'Authorization': f'Bearer {key}'})
    assert r.status_code == 503

def test_public_discovery_stays_public():
    c=TestClient(app)
    for path in ('/quickstart', '/llms.txt', '/skill.md',
                 '/.well-known/agent-card.json', '/.well-known/agent.json',
                 '/openapi.json'):
        assert c.get(path).status_code == 200, path
    card=c.get('/.well-known/agent-card.json').json()
    # A2A v1: bearer auth declared as an httpAuthSecurityScheme
    schemes=card['securitySchemes']
    assert schemes['bearerAuth']['httpAuthSecurityScheme']['scheme'] == 'Bearer'
    assert card['securityRequirements'][0]['schemes']['bearerAuth'] == {'list': []}

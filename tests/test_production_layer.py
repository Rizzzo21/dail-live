import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os
os.environ.pop('DAIL_REAL_PAYMENTS', None)
os.environ.pop('DATABASE_URL', None)
from fastapi.testclient import TestClient
from dail.api import app

def test_public_launch_and_payment_guard():
    c=TestClient(app)
    assert c.get('/').status_code == 200
    assert c.get('/launch').status_code == 200
    s=c.get('/payments/status').json()
    assert s['production_ready'] is False
    r=c.post('/payments/checkout',json={'agent_id':'missing','usd_cents':100})
    assert r.status_code == 503

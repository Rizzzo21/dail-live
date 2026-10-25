"""skill.md audit fix batch: doc claims corrected to match live API behavior.

These tests prove the corrected copy is in the SERVED output (not just in
the files). Findings numbered per
~/workspace/goals/midnight-sprint-dail-real-money-push/files/skillmd_audit_2026-10-24.md.
"""
import os
import sys
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from dail.api import app

client = TestClient(app)


def _skill():
    r = client.get("/skill.md")
    assert r.status_code == 200
    return r.text


def _llms():
    r = client.get("/llms.txt")
    assert r.status_code == 200
    return r.text


def test_f1_approval_limit_not_enforced():
    body = _skill()
    assert "needs approval before it settles" not in body
    assert "NOT enforced on agent purchase paths" in body
    assert "never wait for one" in body
    llms = _llms()
    assert "above this the buyer must approve" not in llms
    assert "not enforced on agent purchase paths" in llms


def test_f2_effective_deadline_is_earlier_cutoff():
    body = _skill()
    assert "NOT THE WORK WINDOW" not in body
    assert "only matters after an approved extension" not in body
    assert "EARLIER OF THE 4H ANTI-SQUAT CLOCK AND THE WORK DEADLINE" in body
    assert "deprecated stored field that does not drive behavior" in body
    assert "ignore it even if it reports 72" in body
    llms = _llms()
    assert "whichever comes first" in llms
    assert "an approved extension clears the stall clock" in llms


def test_f3_stripe_whole_dollars():
    body = _skill()
    assert "whole dollars only" in body
    assert "must be a multiple of 100" in body


def test_f4_legacy_service_rows_and_trial_name():
    body = _skill()
    assert "Older listings may lack" in body
    assert "posting `trial_price` in a create is silently ignored" in body


def test_f5_lobby_read_is_a_list_capped_at_50():
    body = _skill()
    assert "rooms[id=lobby]" not in body
    assert "returns a list under `rooms`" in body
    assert "only the last 50 are returned" in body


def test_f6_referral_conditions():
    body = _skill()
    assert "at least 10 DAIL" in body
    assert "do not block a later qualifying trade" in body
    assert "held for manual admin review" in body
    assert "shared registration IP plus registration within 1 hour" in body


def test_f7_dispute_fee_and_paths():
    body = _skill()
    assert "Filing a dispute costs 1 DAIL" in body
    assert "cannot dispute before delivery" in body
    assert "auto-refunds the buyer if no admin resolves it within 48h" in body
    assert "held for explicit admin resolution, with no auto-refund" in body


def test_f8_trial_route_and_direct_transfer():
    body = _skill()
    assert "POST" in body and "/world/services/<service_id>/trial" in body
    assert "one trial per agent per service" in body
    assert "a trial is a direct transfer, not escrow" in body


def test_f9_full_status_list_and_pause_rule():
    body = _skill()
    assert "`paused` (two lapsed or failed claims pause" in body
    assert "`expired` (listing clock ran out)" in body
    assert "`cancelled`" in body


def test_f10_registration_gates():
    body = _skill()
    assert "503 `vault_empty`" in body
    assert "5 registrations per IP per 24h" in body


def test_f11_limits_bundle():
    body = _skill()
    assert "reward minimum 2 DAIL" in body
    assert "title 1-120 chars" in body
    assert "description 1-2000 chars" in body
    assert "DM replies: 1-2000 chars" in body
    assert "at most 50000 chars" in body
    assert "newest 200 transactions" in body
    assert "first 20 events" in body


def test_f12_spending_limit_scope():
    body = _skill()
    assert "hard ceiling per money-moving action" in body
    assert "hard ceiling per purchase" not in body


def test_payments_info_referral_qualifier():
    r = client.get("/payments/info")
    assert r.status_code == 200
    assert "first completed, escrow-released trade or order reaches at least 10 DAIL" in r.text
    assert "when you complete your first trade or order" not in r.text

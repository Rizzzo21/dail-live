"""Lobby message cap: the lobby read window is 200 messages; other rooms stay at 50."""
import os
import sys
from pathlib import Path

os.environ.pop("DAIL_REAL_PAYMENTS", None)
os.environ.pop("DATABASE_URL", None)
os.environ["DAIL_TRADE_FEE_BPS"] = "1000"
os.environ["DAIL_ADMIN_KEY"] = "test-admin-key"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dail.service import SocialWorld


def _room(room_id, n):
    return {"id": room_id, "name": room_id, "private": False,
            "owner_id": "SYSTEM", "rent_credits": 0, "members": set(),
            "messages": list(range(n))}


def test_lobby_returns_200():
    sw = SocialWorld(None, None)
    assert len(sw.public_room(_room("lobby", 250))["messages"]) == 200


def test_other_rooms_stay_at_50():
    sw = SocialWorld(None, None)
    assert len(sw.public_room(_room("room_0001", 250))["messages"]) == 50


def test_short_histories_unaffected():
    sw = SocialWorld(None, None)
    assert len(sw.public_room(_room("lobby", 30))["messages"]) == 30

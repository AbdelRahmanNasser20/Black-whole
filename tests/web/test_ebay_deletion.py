"""eBay Marketplace Account Deletion endpoint — offline (no DB, no network)."""
import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from automation.web import ebay_deletion
from automation.web.app import app

client = TestClient(app)
ENDPOINT = "https://black-whole.com/ebay/account-deletion"
TOKEN = "blackwhole_ebay_deletion_token_0123456789"
BODY = json.dumps({"metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"},
                   "notification": {"notificationId": "n1",
                                    "data": {"username": "buyer1", "userId": "u-1"}}}).encode()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("EBAY_VERIFICATION_TOKEN", TOKEN)
    monkeypatch.setenv("EBAY_DELETION_ENDPOINT", ENDPOINT)


def _signed(body: bytes):
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    sig = base64.b64encode(priv.sign(body, ec.ECDSA(hashes.SHA1()))).decode()
    header = base64.b64encode(json.dumps({"alg": "ECDSA", "kid": "k1", "signature": sig,
                                          "digest": "SHA1"}).encode()).decode()
    return header, pem


def test_challenge_matches_ebay_formula():
    r = client.get("/ebay/account-deletion", params={"challenge_code": "abc"})
    assert r.status_code == 200
    assert r.json() == {"challengeResponse": hashlib.sha256(
        ("abc" + TOKEN + ENDPOINT).encode()).hexdigest()}


def test_challenge_404_when_unconfigured(monkeypatch):
    monkeypatch.delenv("EBAY_VERIFICATION_TOKEN")
    assert client.get("/ebay/account-deletion", params={"challenge_code": "abc"}).status_code == 404


def test_endpoint_is_public_not_auth_walled():
    assert not ebay_deletion.PATH.startswith(("/api/", "/admin"))


def test_verify_round_trip_and_tamper():
    header, pem = _signed(BODY)
    assert ebay_deletion.verify(BODY, header, get_key=lambda kid: pem)
    assert not ebay_deletion.verify(BODY + b" ", header, get_key=lambda kid: pem)
    assert not ebay_deletion.verify(BODY, None, get_key=lambda kid: pem)


def test_unsigned_notice_is_412_and_deletes_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(ebay_deletion, "delete_user", lambda *a: called.append(a) or 1)
    r = client.post("/ebay/account-deletion", content=BODY)
    assert r.status_code == 412 and not called


def test_verified_notice_deletes_by_username_and_user_id(monkeypatch):
    header, pem = _signed(BODY)
    monkeypatch.setattr(ebay_deletion, "public_key", lambda kid: pem)
    sql = []
    monkeypatch.setattr(ebay_deletion.db, "execute", lambda q, p: sql.append((q, p)) or 1)
    r = client.post("/ebay/account-deletion", content=BODY, headers={"X-EBAY-SIGNATURE": header})
    assert r.status_code == 204
    (q, p), = sql
    assert "platform = 'ebay'" in q and p == (["buyer1", "u-1"],)


def test_failed_delete_returns_500_so_ebay_retries(monkeypatch):
    header, pem = _signed(BODY)
    monkeypatch.setattr(ebay_deletion, "public_key", lambda kid: pem)

    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(ebay_deletion, "delete_user", boom)
    r = client.post("/ebay/account-deletion", content=BODY, headers={"X-EBAY-SIGNATURE": header})
    assert r.status_code == 500


def test_garbage_body_is_acked():
    assert client.post("/ebay/account-deletion", content=b"not json").status_code == 204

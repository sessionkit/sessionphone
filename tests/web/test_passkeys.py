"""The passkey checks, with a software authenticator (PHONE-133 build-001).

Ported from the settings app's cases (settings@19ecdee tests/test_webauth.py):
register and login, then each way a ceremony must fail, then the enrol code.
"""
from __future__ import annotations

import json
import stat

import pytest

from sessionphone.web import passkeys as pk

ORIGIN = "https://host.example.ts.net:8775"


def _chal():
    o, c = pk.offer([], pk.rp_of(ORIGIN), "owner")
    return o["challenge"], c


@pytest.fixture
def ring(tmp_path):
    return pk.Keyring(tmp_path / "web")


def _enrolled(softkey, ring, alg=pk.ES256):
    key = softkey(ORIGIN, alg)
    s, c = _chal()
    cred, why = pk.register(key.create(s), c, ORIGIN)
    assert why == "" and cred
    ring.save(ring.load() + [cred])
    return key


# ---- the offer ------------------------------------------------------------

def test_offer_is_enrol_when_this_rp_has_no_passkey_never_register():
    o, c = pk.offer([], "host.example.ts.net", "owner")
    assert o["mode"] == "enrol" and o["allow"] == [] and o["rpId"] == "host.example.ts.net"
    assert o["user"]["name"] == "owner" and o["user"]["displayName"] == "owner (sessionphone)"
    assert pk.unb64u(o["challenge"]) == c and len(c) == 32


def test_offer_is_login_with_only_this_rps_ids():
    o, _ = pk.offer(["abc"], "host.example.ts.net", "owner")
    assert o["mode"] == "login" and o["allow"] == ["abc"] and "user" not in o


def test_challenges_differ():
    assert pk.offer([], "x", "owner")[1] != pk.offer([], "x", "owner")[1]


def test_rp_of_is_the_host_name_not_the_port():
    assert pk.rp_of("https://host.example.ts.net:8775") == "host.example.ts.net"
    assert pk.rp_of("http://localhost:8775") == "localhost"
    assert pk.rp_of("") == ""


# ---- register and login ---------------------------------------------------

@pytest.mark.parametrize("alg", [pk.ES256, pk.RS256])
def test_register_then_login(softkey, ring, alg):
    key = _enrolled(softkey, ring, alg)
    [cred] = ring.load()
    assert cred["id"] == key.id and cred["rp_id"] == "host.example.ts.net" and cred["alg"] == alg
    s, c = _chal()
    assert pk.login(key.get(s), c, ORIGIN, ring.load()) == ""


@pytest.mark.parametrize("over,why", [
    ({"kind": "webauthn.get"}, "type"),
    ({"origin": "https://evil.example:8775"}, "origin"),
    ({"rp": "evil.example"}, "rpIdHash"),
    ({"flags": pk.UP}, "verification"),
    ({"flags": pk.UV}, "presence"),
    ({"alg": -8}, "algorithm"),
    ({"spki": b"not a key"}, "does not load"),
])
def test_register_refuses(softkey, over, why):
    s, c = _chal()
    cred, got = pk.register(softkey(ORIGIN).create(s, **over), c, ORIGIN)
    assert cred is None and why in got


def test_register_refuses_a_wrong_challenge(softkey):
    s, _ = _chal()
    _, other = _chal()
    assert "challenge" in pk.register(softkey(ORIGIN).create(s), other, ORIGIN)[1]


def test_register_refuses_a_spent_challenge(softkey):
    s, _ = _chal()
    assert "challenge" in pk.register(softkey(ORIGIN).create(s), None, ORIGIN)[1]


def test_register_refuses_a_key_of_the_wrong_type(softkey):
    s, c = _chal()
    rsa_key = softkey(ORIGIN, pk.RS256)
    msg = softkey(ORIGIN).create(s, spki=rsa_key.spki())    # says ES256, carries RSA
    assert "P-256" in pk.register(msg, c, ORIGIN)[1]


@pytest.mark.parametrize("drop", ["clientDataJSON", "authenticatorData", "publicKey", "alg", "id"])
def test_register_refuses_malformed(softkey, drop):
    s, c = _chal()
    msg = softkey(ORIGIN).create(s)
    del msg[drop]
    assert pk.register(msg, c, ORIGIN)[1].startswith("malformed")


def test_register_refuses_short_auth_data(softkey):
    s, c = _chal()
    msg = softkey(ORIGIN).create(s)
    msg["authenticatorData"] = pk.b64u(b"\0" * 10)
    assert "too short" in pk.register(msg, c, ORIGIN)[1]


def test_register_refuses_non_json_client_data(softkey):
    s, c = _chal()
    msg = softkey(ORIGIN).create(s)
    msg["clientDataJSON"] = pk.b64u(b"[1, 2")
    assert "not JSON" in pk.register(msg, c, ORIGIN)[1]


@pytest.mark.parametrize("over,why", [
    ({"kind": "webauthn.create"}, "type"),
    ({"origin": "https://evil.example:8775"}, "origin"),
    ({"rp": "evil.example"}, "rpIdHash"),
    ({"flags": pk.UP}, "verification"),
    ({"tamper": True}, "signature"),
    ({"cid": "nobody"}, "unknown passkey"),
])
def test_login_refuses(softkey, ring, over, why):
    key = _enrolled(softkey, ring)
    s, c = _chal()
    assert why in pk.login(key.get(s, **over), c, ORIGIN, ring.load())


def test_login_refuses_a_wrong_challenge(softkey, ring):
    key = _enrolled(softkey, ring)
    s, _ = _chal()
    _, other = _chal()
    assert "challenge" in pk.login(key.get(s), other, ORIGIN, ring.load())


def test_login_refuses_another_rps_passkey(softkey, ring):
    """A passkey enrolled for the tailnet name does not open localhost."""
    key = _enrolled(softkey, ring)
    s, c = _chal()
    assert pk.login(key.get(s, origin="http://localhost:8775"), c, "http://localhost:8775",
                    ring.load()) == "unknown passkey"


def test_login_refuses_a_different_key_under_a_known_id(softkey, ring):
    key = _enrolled(softkey, ring)
    impostor = softkey(ORIGIN)
    impostor.id = key.id
    s, c = _chal()
    assert "signature" in pk.login(impostor.get(s), c, ORIGIN, ring.load())


def test_login_refuses_malformed(softkey, ring):
    key = _enrolled(softkey, ring)
    s, c = _chal()
    msg = key.get(s)
    del msg["signature"]
    assert pk.login(msg, c, ORIGIN, ring.load()).startswith("malformed")


# ---- inside a frame: crossOrigin (PHONE-139, design §5.5) -----------------

def test_register_refuses_a_ceremony_made_inside_a_frame(softkey):
    s, c = _chal()
    cred, why = pk.register(softkey(ORIGIN).create(s, cross=True), c, ORIGIN)
    assert cred is None and why.startswith("crossOrigin is true")


def test_login_refuses_an_assertion_made_inside_a_frame(softkey, ring):
    key = _enrolled(softkey, ring)
    s, c = _chal()
    assert pk.login(key.get(s, cross=True), c, ORIGIN, ring.load()).startswith("crossOrigin is true")


@pytest.mark.parametrize("cross", [{"cross": False}, {}], ids=["false", "absent"])
def test_crossorigin_false_or_absent_works_as_before(softkey, ring, cross):
    key = softkey(ORIGIN)
    s, c = _chal()
    msg = key.create(s, **cross)
    assert ("crossOrigin" in json.loads(pk.unb64u(msg["clientDataJSON"]))) == bool(cross)
    cred, why = pk.register(msg, c, ORIGIN)
    assert why == "" and cred
    ring.save([cred])
    s, c = _chal()
    assert pk.login(key.get(s, **cross), c, ORIGIN, ring.load()) == ""


@pytest.mark.parametrize("cross", ["true", 1, 0, "", [], {}])
def test_crossorigin_that_is_not_a_boolean_fails_closed(softkey, ring, cross):
    key = _enrolled(softkey, ring)
    s, c = _chal()
    assert "not a boolean" in pk.register(softkey(ORIGIN).create(s, cross=cross), c, ORIGIN)[1]
    s, c = _chal()
    assert "not a boolean" in pk.login(key.get(s, cross=cross), c, ORIGIN, ring.load())


def test_a_wrong_origin_is_still_named_before_crossorigin(softkey):
    """The existing refusals keep their order: origin is checked first."""
    s, c = _chal()
    why = pk.register(softkey(ORIGIN).create(s, origin="https://evil.example", cross=True), c, ORIGIN)[1]
    assert why.startswith("origin ")


# ---- the store ------------------------------------------------------------

def test_store_is_private_and_round_trips(ring):
    ring.save([{"id": "a", "rp_id": "x"}])
    assert ring.load() == [{"id": "a", "rp_id": "x"}]
    assert stat.S_IMODE(ring.store.stat().st_mode) == 0o600
    assert stat.S_IMODE(ring.dir.stat().st_mode) == 0o700
    assert ring.mine("x") == ["a"] and ring.mine("y") == []


def test_store_missing_is_empty(ring):
    assert ring.load() == []


# ---- the enrol code -------------------------------------------------------

def test_enrol_code_keeps_only_a_salted_hash(ring):
    code = ring.new_enrol_code()
    assert len(code) == pk.ENROL_LEN and set(code) <= set(pk.ENROL_ALPHABET)
    text = ring.enrol.read_text()
    assert code not in text and set(json.loads(text)) == {"salt", "hash", "expires", "wrong"}
    assert stat.S_IMODE(ring.enrol.stat().st_mode) == 0o600


def test_enrol_code_is_single_use(ring):
    code = ring.new_enrol_code()
    assert ring.check_enrol_code(code) == ""
    assert "no enrol code is pending" in ring.check_enrol_code(code)


def test_enrol_code_is_typed_loosely(ring):
    code = ring.new_enrol_code()
    assert ring.check_enrol_code(f" {code[:5].lower()}-{code[5:]} ") == ""


def test_precheck_does_not_spend_a_right_code(ring):
    code = ring.new_enrol_code()
    assert ring.check_enrol_code(code, consume=False) == ""
    assert ring.check_enrol_code(code, consume=False) == ""
    assert json.loads(ring.enrol.read_text())["wrong"] == 0
    assert ring.check_enrol_code(code) == ""


def test_precheck_counts_a_wrong_code(ring):
    ring.new_enrol_code()
    assert "4 tries left" in ring.check_enrol_code("WRONGWRONG", consume=False)


def test_five_wrong_tries_burn_the_code(ring):
    code = ring.new_enrol_code()
    for left in (4, 3, 2, 1):
        assert f"{left} tries left" in ring.check_enrol_code("WRONGWRONG")
    assert "burnt" in ring.check_enrol_code("WRONGWRONG")
    assert "no enrol code is pending" in ring.check_enrol_code(code)


def test_empty_code_counts_as_wrong(ring):
    ring.new_enrol_code()
    assert "tries left" in ring.check_enrol_code("")


def test_enrol_code_expires(ring):
    code = ring.new_enrol_code(now=1000.0)
    assert ring.check_enrol_code(code, now=1000.0 + pk.ENROL_TTL - 1, consume=False) == ""
    assert "expired" in ring.check_enrol_code(code, now=1000.0 + pk.ENROL_TTL)
    assert not ring.enrol.exists()


def test_a_new_code_replaces_the_old(ring):
    old = ring.new_enrol_code()
    new = ring.new_enrol_code()
    assert "wrong" in ring.check_enrol_code(old) and ring.check_enrol_code(new) == ""


def test_unreadable_enrol_file_fails_closed(ring):
    ring.dir.mkdir(parents=True)
    ring.enrol.write_text("{not json")
    assert "no enrol code is pending" in ring.check_enrol_code("ANYTHING22")
    assert not ring.enrol.exists()


def test_no_code_pending(ring):
    assert "sessionphone web enrol" in ring.check_enrol_code("ANYTHING22")

"""The call page (PHONE-133 design §6), read as text: the browser drive
(`make browser`, tests/browser/) is what runs it. These pin what must hold
whether or not Chrome is on the machine."""
from __future__ import annotations

import re

from sessionphone.web.server import page_bytes

PAGE = page_bytes().decode()


def test_the_page_is_one_file_with_no_external_fetch():
    """Inline JS and CSS only: no script, style, image or font from anywhere."""
    assert not re.search(r"""<(script|link|img|iframe)[^>]*(src|href)=""", PAGE)
    assert not re.search(r"https?://", PAGE)
    assert "import " not in PAGE and "fetch(" not in PAGE


def test_two_taps_sign_in_then_call():
    assert 'id="signin"' in PAGE and 'id="call"' in PAGE
    assert "navigator.credentials.get" in PAGE and "navigator.credentials.create" in PAGE
    assert '"Hang up"' in PAGE and '{ type: "hangup" }' in PAGE


def test_the_socket_is_the_pages_own_host():
    assert 'location.host + "/ws"' in PAGE and '"wss://"' in PAGE


def test_no_stun_and_no_video():
    assert "iceServers: []" in PAGE
    assert "video" not in PAGE and "getDisplayMedia" not in PAGE


def test_a_passkey_needs_user_verification_and_sends_no_attestation():
    assert PAGE.count('userVerification: "required"') == 2
    assert 'attestation: "none"' in PAGE
    assert "getPublicKey()" in PAGE and "getPublicKeyAlgorithm()" in PAGE


def test_every_server_message_is_handled():
    """design §5: what the server may send, the page answers."""
    for kind in ("auth", "enrol-ok", "enrol-failed", "auth-failed", "refused", "auth-ok",
                 "answer", "busy", "error", "ended"):
        assert f'm.type === "{kind}"' in PAGE, kind


def test_the_page_says_nothing_of_mail():
    """--to was removed (do-002): a web call mails no one."""
    assert "--to" not in PAGE and "mail" not in PAGE.lower()


def test_ice_waits_for_the_answer_so_busy_is_not_hidden():
    """A busy line has no call: ICE sent before "busy" draws an "error: no call" that
    would overwrite the busy reason. So the held ICE is flushed on "answer" only."""
    answer = PAGE.index('m.type === "answer"')
    flush = PAGE.index("held.splice(0).forEach(send)")
    assert PAGE.count("held.splice(0).forEach(send)") == 1 and answer < flush < PAGE.index('m.type === "busy"')


def test_the_enrol_code_from_the_qr_is_read_once_and_struck_at_once():
    """PHONE-136 R2: `/#code=<code>` is read ONCE, before the socket, and the
    fragment is replaced out of the address bar and the history straight away."""
    assert PAGE.count("location.hash") == 1
    strip = 'history.replaceState(null, "", location.pathname + location.search)'
    assert PAGE.count(strip) == 1 and "pushState" not in PAGE
    assert PAGE.index("location.hash") < PAGE.index(strip) < PAGE.index("function connect()")
    assert 'h.has("code")' in PAGE                     # the key enrol_qr.FRAGMENT_KEY puts there


def test_the_code_from_the_qr_is_filled_after_lockeds_reset_and_only_once():
    """locked() empties the field on every offer: the fill comes AFTER that, in
    enrol mode only, and the held code is then forgotten, so a later offer (after
    a failure) does not refill it."""
    body = PAGE[PAGE.index("function locked()"):PAGE.index("function ready(")]
    reset, fill, forget = body.index('$("code").value = ""'), body.index('$("code").value = fromQr'), \
        body.index('fromQr = ""')
    assert reset < fill < forget
    assert "if (enrol) {" in body[reset:fill + 40]
    assert PAGE.count('$("code").value = fromQr') == 1

"""The carrier's source is named by what sessionmail actually used (PHONE-214).

sessionmail reads `sendmail` from two file layers: its INSTALLED template
(`mail.template.json`, source `template`, written at its install and holding
`"sendmail": "drop"`) and the user's own `mail.json` (source `host`), which
wins when it sets the key. A fresh install's mail.json holds only `hooks`, so
the drop comes from the template: sessionmail's default, never "from mail.json".

Every line that names the carrier's source says the same thing for each
source: the `seat:` line (seatmail.signing), the `mail:` line and the bot's
start line (seatmail.carrier_said), and the settings block's row
(settings.mail_note). Fakes only: sessionmail's answers are stubbed.
"""
import pytest
from test_first_seat import _install, _no_signer, rig  # noqa: F401 - rig is a fixture

from sessionphone import service, settings
from sessionphone.bridge import seatmail

MAIL_JSON = "~/.sessionkit/mail/settings/mail.json"
TEMPLATE = "~/.sessionkit/mail/settings/mail.template.json"
FROM_HOST = f"sessionmail's {MAIL_JSON} `sendmail`"
FROM_TEMPLATE = f"sessionmail's installed default, {TEMPLATE} `sendmail`"
FROM_DEFAULT = "sessionmail's default"
FROM_VARIABLE = "SESSIONMAIL_SENDMAIL"

# source -> the words every line uses for it
WORDS = {"variable": FROM_VARIABLE, "host": FROM_HOST, "template": FROM_TEMPLATE,
         "default": FROM_DEFAULT}


def _probe(sendmail):
    """settings.mail_file's answer: SENDMAIL over 'every other key unset'."""
    return {"sendmail": sendmail, "root": (None, "default"), "host": (None, "default"),
            "postfix.relay_domain": (None, "default")}


def _ans(kind, source, path=None):
    """settings.mail_carrier's answer, as sessionmail's resolver gives it."""
    return {"kind": kind, "path": path, "source": source, "why": ""}


def test_the_paths_are_the_kits():
    assert settings.shown(settings.MAIL_FILE) == MAIL_JSON
    assert settings.shown(settings.MAIL_TEMPLATE) == TEMPLATE


@pytest.mark.parametrize("source", ["host", "template", "default"])
def test_mail_from_names_each_layer(source):
    assert settings.mail_from("sendmail", source) == WORDS[source]


def test_only_the_host_layer_is_called_mail_json():
    """The bug: the template layer was called mail.json too."""
    assert "mail.json" not in settings.mail_from("sendmail", "template").replace("mail.template.json", "")
    assert "default" in settings.mail_from("sendmail", "template")
    assert "default" not in settings.mail_from("sendmail", "host")


# ---- the mail: line's carrier (carrier_said), every source ----------------------

@pytest.mark.parametrize("source", ["host", "template", "default"])
@pytest.mark.parametrize("value, said", [("drop", "drop"), ("/opt/mta/sendmail", "MTA /opt/mta/sendmail")])
def test_carrier_said_names_the_layer_used(source, value, said):
    kind, words = seatmail.carrier_said({}, _probe((value, source)))
    assert (kind, words) == (seatmail.BY, f"{said} ({WORDS[source]})")


def test_carrier_said_the_variable_is_named_as_itself():
    assert seatmail.carrier_said({"SESSIONMAIL_SENDMAIL": "drop"}, _probe(("drop", "template")))[1] == \
        "drop (SESSIONMAIL_SENDMAIL=drop, no MTA)"
    assert seatmail.carrier_said({"SESSIONMAIL_SENDMAIL": "/x/mta"}, _probe(("drop", "host")))[1] == \
        "MTA /x/mta (SESSIONMAIL_SENDMAIL)"


def test_a_template_value_that_is_no_carrier_names_the_template_file():
    kind, words = seatmail.carrier_said({}, _probe(("smtp", "template")))
    assert kind == seatmail.NOWHERE
    assert words.startswith(f'nowhere: {TEMPLATE} `sendmail` is "smtp"')
    kind, words = seatmail.carrier_said({}, _probe(("smtp", "host")))
    assert words.startswith(f'nowhere: {MAIL_JSON} `sendmail` is "smtp"')


@pytest.mark.parametrize("source", ["variable", "host", "template", "default"])
def test_carrier_said_of_the_resolvers_answer_names_the_layer(monkeypatch, source):
    """With no file answer, carrier_said asks sessionmail's resolver (_said_of)."""
    monkeypatch.setattr(settings, "mail_file", lambda exe: None)
    monkeypatch.setattr(settings, "mail_carrier", lambda exe, v=None: _ans("drop", source))
    monkeypatch.setattr(seatmail, "resolve_message", lambda e=None: "/x/message")
    assert seatmail.carrier({}) == f"drop ({WORDS[source]})"


# ---- the seat: line's reason (signing), every source ----------------------------

@pytest.mark.parametrize("source", ["variable", "host", "template", "default"])
def test_signing_names_the_layer_used(monkeypatch, source):
    for k in ("DC_SIGNER_SOCK", "DC_SIGNER_PUB", "PIP_CHAT_SIGNER_SOCK", "PIP_CHAT_SIGNER_PUB"):
        monkeypatch.delenv(k, raising=False)
    assert seatmail.signing({}, _ans("drop", source)) == (
        seatmail.UNSIGNED, f"carrier drop: this bot writes into the box itself, from {WORDS[source]}")
    assert seatmail.signing({}, _ans("mta", source, "/usr/sbin/sendmail")) == (
        seatmail.SIGNED, f"carrier /usr/sbin/sendmail is an MTA, from {WORDS[source]}")


# ---- the mail: line's advice ----------------------------------------------------

def test_a_template_mta_is_told_to_write_the_drop_in_mail_json():
    """mail.json beats the template, so writing the key there is the right fix."""
    line = service.mail_line({"DC_AGENT": "ada"}, probe=_probe(("/usr/sbin/sendmail", "template")))
    assert f"goes by MTA /usr/sbin/sendmail ({FROM_TEMPLATE})" in line
    assert f'write "sendmail": "drop" in {MAIL_JSON}' in line


def test_a_template_drop_needs_no_advice():
    line = service.mail_line({"DC_AGENT": "ada"}, probe=_probe(("drop", "template")))
    assert line.endswith(f"the bot's mail goes by drop ({FROM_TEMPLATE})")


# ---- the settings block's row (mail_note) ---------------------------------------

def test_the_settings_row_names_the_template_not_mail_json():
    probe = _probe(("drop", "template"))
    assert settings.mail_note("SESSIONMAIL_SENDMAIL", None, probe) == \
        f"{FROM_TEMPLATE} (not carried: the bot's `message` reads it)"
    assert settings.mail_note("SESSIONMAIL_SENDMAIL", "drop", probe) == \
        f"this shell (not saved here: {FROM_TEMPLATE} holds it too)"
    note = settings.mail_note("SESSIONMAIL_SENDMAIL", "/x/mta", probe)
    assert f'write "sendmail": "/x/mta" in {MAIL_JSON}' in note
    assert f'it has no such key; {FROM_TEMPLATE} is "drop"' in note
    assert "it holds" not in note


def test_the_settings_row_for_mail_json_is_unchanged():
    probe = _probe(("drop", "host"))
    assert settings.mail_note("SESSIONMAIL_SENDMAIL", None, probe) == \
        f"{FROM_HOST} (not carried: the bot's `message` reads it)"
    assert settings.mail_note("SESSIONMAIL_SENDMAIL", "drop", probe) == \
        f"this shell (not saved here: {FROM_HOST} holds it too)"
    assert 'it holds "drop"' in settings.mail_note("SESSIONMAIL_SENDMAIL", "/x/mta", probe)


# ---- bot install end to end: the walk's fresh install, and mail.json set ----------

def _lines(r, tag):
    return [ln for ln in r.output.splitlines() if ln.startswith(tag)]


@pytest.mark.parametrize("filed, words", [
    (("drop", "template"), FROM_TEMPLATE),      # the walk: mail.json holds only hooks
    (("drop", "host"), FROM_HOST),              # mail.json sets it, over the template
    (("drop", "default"), FROM_DEFAULT),        # no layer sets it
], ids=["template", "host-over-template", "default"])
def test_install_seat_and_mail_lines_name_the_layer_used(rig, monkeypatch, filed, words):  # noqa: F811
    _no_signer(monkeypatch)
    monkeypatch.setattr(settings, "mail_file", lambda exe: _probe(filed))
    r = _install()
    assert r.exit_code == 0, r.output
    [seat_line] = _lines(r, "seat:")
    assert seat_line == (f"seat:      claude-chat, unsigned (carrier drop: this bot writes into the "
                         f"box itself, from {words}), no key")
    [mail] = _lines(r, "mail:")
    assert f"the bot's mail goes by drop ({words})" in mail
    if filed[1] != "host":
        assert MAIL_JSON + " `sendmail`" not in seat_line + mail

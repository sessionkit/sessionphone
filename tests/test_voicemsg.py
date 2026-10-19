"""The voice-message checker (PHONE-118, design.md decision 3; review R2, R3).

Per suffix: the first bytes must agree. Empty, text and mislabelled files are
refused, and so is a UTF-16LE byte-order mark that an `FF Ex` rule would pass.
"""
from __future__ import annotations

import pytest

from sessionphone import voicemsg

PAD = b"\x00" * 16

# suffix -> first bytes of a real file of that kind (as measured in the design:
# say, afconvert and ffmpeg output), padded past the HEAD read
GOOD = {
    ".ogg": b"OggS\x00\x02" + PAD,
    ".oga": b"OggS\x00\x02" + PAD,
    ".m4a": b"\x00\x00\x00\x1cftypM4A " + PAD,
    ".mp3": b"ID3\x04\x00" + PAD,
    ".aac": b"\xff\xf1\x50\x80" + PAD,              # ADTS, MPEG-4
    ".wav": b"RIFF\x24\x08\x00\x00WAVEfmt " + PAD,
    ".flac": b"fLaC\x00\x00\x00\x22" + PAD,
}


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_the_suffixes_are_the_cores_seven_audio_types():
    assert voicemsg.SUFFIXES == (".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".wav")


@pytest.mark.parametrize("suffix", sorted(GOOD))
def test_each_suffix_with_its_own_signature_passes(tmp_path, suffix):
    assert voicemsg.problem(write(tmp_path, "note" + suffix, GOOD[suffix])) is None


@pytest.mark.parametrize("suffix", sorted(GOOD))
def test_the_suffix_is_matched_in_any_case(tmp_path, suffix):
    assert voicemsg.problem(write(tmp_path, "NOTE" + suffix.upper(), GOOD[suffix])) is None


@pytest.mark.parametrize("data", [
    b"\xff\xfb\x90\x64",        # MPEG-1 layer III
    b"\xff\xf3\x64\xc4",        # MPEG-2 layer III (measured: FF F3)
    b"\xff\xe3\x18\xc4",        # MPEG-2.5 layer III
])
def test_an_mp3_may_start_with_a_bare_layer_three_frame(tmp_path, data):
    assert voicemsg.problem(write(tmp_path, "note.mp3", data + PAD)) is None


def test_an_aac_may_start_with_id3(tmp_path):
    assert voicemsg.problem(write(tmp_path, "note.aac", b"ID3\x04\x00" + PAD)) is None


def test_the_measured_adts_sync_passes(tmp_path):
    assert voicemsg.problem(write(tmp_path, "note.aac", b"\xff\xf9\x50\x80" + PAD)) is None


@pytest.mark.parametrize("suffix", [".mp3", ".aac"])
def test_a_utf16le_byte_order_mark_is_refused(tmp_path, suffix):
    # FF FE passes "FF then a byte of E0 or more"; the layer bits refuse it (R3)
    p = write(tmp_path, "note" + suffix, "hello".encode("utf-16"))
    assert p.read_bytes()[:2] == b"\xff\xfe"
    assert "first bytes" in voicemsg.problem(p)


@pytest.mark.parametrize("data", [b"\xff\xf1\x50\x80", b"\xff\xfd\x90\x64"])  # ADTS; layer I
def test_an_mp3_refuses_a_frame_that_is_not_layer_three(tmp_path, data):
    assert "first bytes" in voicemsg.problem(write(tmp_path, "note.mp3", data + PAD))


def test_an_aac_refuses_an_mp3_frame(tmp_path):
    assert "first bytes" in voicemsg.problem(write(tmp_path, "note.aac", b"\xff\xfb\x90\x64" + PAD))


@pytest.mark.parametrize("suffix", sorted(GOOD))
def test_an_empty_file_is_refused(tmp_path, suffix):
    assert voicemsg.problem(write(tmp_path, "note" + suffix, b"")) == "the file is empty"


@pytest.mark.parametrize("suffix", sorted(GOOD))
def test_text_under_an_audio_suffix_is_refused(tmp_path, suffix):
    why = voicemsg.problem(write(tmp_path, "note" + suffix, b"TTS failed: no voice named af_sky\n"))
    assert why.startswith(f"not {suffix[1:]} audio")


def test_a_mislabelled_container_is_refused(tmp_path):
    # Ogg bytes named .m4a: the core takes the MIME from the suffix
    assert "ftyp" in voicemsg.problem(write(tmp_path, "note.m4a", GOOD[".ogg"]))


def test_a_wav_needs_wave_at_byte_eight(tmp_path):
    assert "RIFF....WAVE" in voicemsg.problem(write(tmp_path, "note.wav", b"RIFF\x24\x08\x00\x00AVI " + PAD))


@pytest.mark.parametrize("name", ["notes.txt", "note.opus", "note.spx", "note.weba", "note"])
def test_other_suffixes_are_refused_and_the_accepted_ones_named(tmp_path, name):
    why = voicemsg.problem(write(tmp_path, name, GOOD[".ogg"]))
    assert "is not a voice-message suffix" in why
    assert voicemsg.accepted() in why


def test_a_missing_file_is_refused(tmp_path):
    assert voicemsg.problem(tmp_path / "gone.ogg") == "no such file"


def test_a_directory_is_refused(tmp_path):
    (tmp_path / "dir.ogg").mkdir()
    assert voicemsg.problem(tmp_path / "dir.ogg") == "not a regular file"


def test_main_passes_audio_silently(tmp_path, capsys):
    p = write(tmp_path, "note.ogg", GOOD[".ogg"])
    assert voicemsg.main(["check", "--tool", "dc-send-file", str(p)]) == 0
    assert capsys.readouterr() == ("", "")


def test_main_refuses_with_exit_1_naming_the_file_and_the_suffixes(tmp_path, capsys):
    p = write(tmp_path, "notes.txt", b"hello\n")
    assert voicemsg.main(["check", "--tool", "dc-send-file", str(p)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("dc-send-file: ")
    assert str(p) in err
    assert voicemsg.accepted() in err


def test_main_usage_is_exit_2():
    with pytest.raises(SystemExit) as e:
        voicemsg.main([])
    assert e.value.code == 2

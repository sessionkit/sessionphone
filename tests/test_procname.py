"""The bots run as <venv>/libexec/session-phone: a hard link, re-made when stale (PHONE-80)."""
import os

from sessionphone.procname import named_python


def venv(tmp_path, body=b"interpreter one"):
    v = tmp_path / "venv"
    (v / "bin").mkdir(parents=True)
    (v / "pyvenv.cfg").write_text("home = /x\n")
    real = tmp_path / "python3.13"
    real.write_bytes(body)
    real.chmod(0o755)
    (v / "bin" / "python").symlink_to(real)
    return v, real


def test_makes_a_hard_link_named_for_the_role(tmp_path):
    v, real = venv(tmp_path)
    got = named_python(python=str(v / "bin" / "python"))
    assert got == str(v / "libexec" / "session-phone")
    assert not os.path.islink(got)                       # a symlink would show the target's name
    assert os.stat(got).st_ino == real.stat().st_ino


def test_relinks_when_bin_python_moves_on(tmp_path):
    v, _ = venv(tmp_path)
    link = named_python(python=str(v / "bin" / "python"))
    newer = tmp_path / "python3.13.9"
    newer.write_bytes(b"interpreter two")
    (v / "bin" / "python").unlink()
    (v / "bin" / "python").symlink_to(newer)
    assert named_python(python=link) == link              # called as the link itself, as the bot is
    assert os.stat(link).st_ino == newer.stat().st_ino


def test_a_fresh_link_is_left_alone(tmp_path):
    v, _ = venv(tmp_path)
    link = named_python(python=str(v / "bin" / "python"))
    before = os.stat(link).st_mtime_ns, os.stat(link).st_ino
    named_python(python=link)
    assert (os.stat(link).st_mtime_ns, os.stat(link).st_ino) == before


def test_not_a_venv_or_no_link_keeps_the_interpreter(tmp_path, monkeypatch):
    lone = tmp_path / "bin" / "python"
    lone.parent.mkdir()
    lone.write_bytes(b"x")
    assert named_python(python=str(lone)) == str(lone)
    v, _ = venv(tmp_path)

    def cross_device(*a):
        raise OSError(18, "Cross-device link")
    monkeypatch.setattr(os, "link", cross_device)
    assert named_python(python=str(v / "bin" / "python")) == str(v / "bin" / "python")


def test_the_ruled_name_fits_what_macos_shows():
    """The name is session-phone. macOS shows 16 characters."""
    import inspect
    name = inspect.signature(named_python).parameters["name"].default
    assert name == "session-phone" and len(name) <= 15

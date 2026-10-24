"""displayname - the bot's Delta Chat display name follows its agent.

The rule, in order:

- DC_NAME, when set, is the name.
- Else the name is the agent's name, title-cased (`versa` -> `Versa`), but only
  over a name nobody chose: an empty one, or the title-cased name of an agent
  this root served. "Served" is narrow: the bot's own agent, and the agent
  `bot install --replace OLD` retired, nothing older. Matched without regard to
  case, so `claude` and `Claude` are both the default for `claude`.
- Any other name is the user's (`sessionphone setup --name "Ada Lovelace"`) and
  is kept. `bot install` says so, with the DC_NAME line that changes it.

What is stored, and where: `bot install --replace OLD` writes the retired
agent's name (one per line, for each --replace) to `name-replaced` under the
root, 0600. The bot reads it at its start and removes it once the name is
settled, so nothing is remembered beyond that one replace.

Who applies it: the bot, at its start (it holds the account then), before its
`online as` line, and only when the name changes. It then writes the saved
invite again (`invite.txt` under the root): the invite carries the name (`n=`),
and `invite`/`qr` read it, so they show the new name. Each start logs one
`name:` line when it changes the name (old -> new) or keeps the user's.
"""
from __future__ import annotations

import os
import pathlib

from sessionphone import account as account_mod

REPLACED = "name-replaced"      # under the root: the agent `bot install --replace` retired
INVITE = "invite.txt"           # under the root: the invite `setup` saved


def default_for(agent: str) -> str:
    """The name a bot for AGENT has when nobody chose one: AGENT title-cased."""
    return agent.strip().title()


def _is_default(name: str, agents) -> bool:
    n = name.strip().casefold()
    return any(n == default_for(a).casefold() for a in agents if a.strip())


def wanted(current: str, agent: str, chosen: str = "", replaced=()) -> str | None:
    """The name the bot should carry, or None to keep CURRENT (the user's).
    CHOSEN is DC_NAME; REPLACED the agents a --replace retired."""
    chosen = (chosen or "").strip()
    if chosen:
        return chosen
    if not (current or "").strip() or _is_default(current, (agent, *replaced)):
        return default_for(agent)
    return None


def change_line(name: str) -> str:
    """The command that gives the bot NAME, single-quoted for a shell."""
    q = "'" + name.replace("'", "'\\''") + "'"
    return f"DC_NAME={q} sessionphone bot install"


# ---- the record of the retired agent ---------------------------------------


def replaced_path(root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(root) / REPLACED


def record_replaced(root: pathlib.Path, agents) -> None:
    """`bot install --replace`: write the retired AGENTS under ROOT, 0600,
    replacing any older record. Nothing is written for no agents. Raises OSError."""
    names = [a.strip().lower() for a in agents if a.strip()]
    if not names:
        return
    p = replaced_path(root)
    tmp = p.with_name(p.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, ("".join(f"{n}\n" for n in names)).encode())
    finally:
        os.close(fd)
    os.replace(tmp, p)


def read_replaced(root: pathlib.Path) -> tuple[str, ...]:
    """The agents the last --replace retired, or () when there is no record
    (or it cannot be read: then no old name is taken as a default)."""
    try:
        text = replaced_path(root).read_text()
    except OSError:
        return ()
    return tuple(n.strip().lower() for n in text.splitlines() if n.strip())


def forget_replaced(root: pathlib.Path) -> None:
    try:
        replaced_path(root).unlink()
    except FileNotFoundError:
        pass


# ---- the bot's start --------------------------------------------------------


def _write_invite(p: pathlib.Path, link: str) -> None:
    tmp = p.with_name(p.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (link + "\n").encode())
    finally:
        os.close(fd)
    os.replace(tmp, p)


def _saved_name(p: pathlib.Path) -> str | None:
    """The `n=` of the invite saved at P; None when there is none to read."""
    try:
        return account_mod.invite_name(p.read_text().strip())
    except OSError:
        return None


def at_start(acct, root: pathlib.Path, agent: str, chosen: str = "") -> str | None:
    """The bot's start, holding ACCT: give it its name (`wanted`) and, when the
    name changed or the saved invite carries another, write the invite again.
    -> the one line to log, or None when there is nothing to say. The record of
    the retired agent is removed once this is done. Raises whatever core
    raises (the record is kept then); the bot logs it and carries on."""
    root = pathlib.Path(root)
    current = str(acct.get_config("displayname") or "")
    new = wanted(current, agent, chosen, read_replaced(root))
    if new is None:
        said = (f"name: kept {current!r}, not the agent's name, so taken as set by hand; "
                f"to change it: {change_line(default_for(agent))}")
        final = current
    elif new != current:
        acct.set_config("displayname", new)
        why = "DC_NAME" if (chosen or "").strip() else "the agent's name"
        said = f"name: {current!r} -> {new!r} ({why})" if current else f"name: set {new!r} ({why})"
        final = new
    else:
        said, final = None, current
    invite = root / INVITE
    saved = _saved_name(invite)
    if (new is not None and new != current) or (saved is not None and saved != final):
        _write_invite(invite, acct.get_qr_code())
        if said:
            said += "; the saved invite is updated"
        else:
            said = f"name: the saved invite said {saved!r}; it is updated to {final!r}"
    forget_replaced(root)
    return said


# ---- bot install's line ------------------------------------------------------


def install_line(root: pathlib.Path, agent: str, chosen: str = "", replaced=()) -> str:
    """`bot install`'s `name:` line, judged on the invite saved under ROOT
    (the account itself cannot be read while the old bot holds it)."""
    current = _saved_name(pathlib.Path(root) / INVITE)
    if (chosen or "").strip():
        return f"name:      {chosen.strip()} (DC_NAME), set by the bot at its start"
    title = default_for(agent)
    if current is None:
        return (f"name:      {title}, the agent's name, set by the bot at its start unless the "
                f"account has a name of its own (DC_NAME sets one)")
    new = wanted(current, agent, chosen, replaced)
    if new is None:
        return (f"name:      kept {current!r}: it is not the agent's name, so it is taken as yours. "
                f"To give the bot its agent's name: {change_line(title)}")
    if new == current:
        return f"name:      {new}, the agent's name (DC_NAME sets another)"
    return (f"name:      {current or '(none)'} -> {new}, the agent's name, set by the bot at its "
            f"start (DC_NAME sets another)")


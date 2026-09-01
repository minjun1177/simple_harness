"""Where the harness keeps what outlives a session.

Everything personal - the conversations, the long-term memory, the input
history, the saved API keys - lives under one directory, `~/.localchat`.

It used to be split. Keys went to `~/.localchat`, and `sessions/`,
`memory.json` and `.chat_history` were written into whatever directory the
harness happened to start in. That was survivable while it was run as
`python app.py` from its own checkout, and stopped being survivable the moment
it became a command you can run anywhere: starting it in a home directory left
files there, starting it in two projects gave you two unrelated memories, and
`/sessions` only ever listed the ones belonging to wherever you were standing.

What stays per-directory is the part that is genuinely about a project rather
than about you: `.permissions.json`, `.mcp.json` and `skills/`, each of which
is read from the working directory first and from here second.

`LOCALCHAT_HOME` overrides the location - useful for keeping two profiles
apart, and how the tests get a directory of their own instead of the real one.

Stdlib only, and it imports nothing local. `config` builds the system prompt at
import time (ARCHITECTURE 5.2), so the modules below it cannot import `config`;
they can import this.
"""

import os

ENV_VAR = "LOCALCHAT_HOME"
DIR_NAME = ".localchat"

# Written into the working directory by versions before this one.
LEGACY_IN_CWD = ("memory.json", "sessions", ".chat_history")


def home() -> str:
    """The directory holding everything that outlives a session."""
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), DIR_NAME)


def state(*parts: str) -> str:
    """A path inside `home()`. Nothing is created; the writer does that."""
    return os.path.join(home(), *parts)


def ensure_home() -> str:
    """`home()`, created if it is not there yet. Returns it either way."""
    directory = home()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass          # the writer will report it with the file it was writing
    return directory


def strays_in_cwd() -> list:
    """Names in the working directory left by an older version, if any.

    Only reported, never touched. `sessions` is an ordinary enough directory
    name that moving one on sight would eventually destroy somebody's actual
    work - so this says what it found and lets the person decide.
    """
    return [name for name in LEGACY_IN_CWD if os.path.exists(name)]

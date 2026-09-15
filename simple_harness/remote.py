"""This session, from the phone in your pocket.

A harness is a terminal, and a terminal is somewhere you have to be. That is
fine while the answer takes four seconds and wrong the moment it takes four
minutes: a `/deepthink` pass, a test suite the model is chasing, a sub-agent
reading half a repository. The work is happening on this machine and the
person has left the desk, so the two things they actually need - *what is it
doing* and *yes, go ahead* - are exactly the two things they cannot reach.

So the harness can hand out one door into the session it is already running.
`/remote on` opens an HTTP server on this machine, prints a URL with a token
in it, and anything that can open that URL is now at the prompt: it sees the
transcript as it is printed, types lines that arrive as if they had been typed
here, and answers the approval prompts that would otherwise sit waiting for a
keystroke from somebody who is on a train.

**It is a door into a shell.** Whoever holds the link can run what the person
at this keyboard can run. That is the feature, and it is also the whole of the
risk, so:

  * it is **off by default** and starts only on an explicit `/remote on`;
  * the token is generated per start, never saved, and never written into
    `settings.json` - there is no long-lived credential to leak. Loopback gets
    128 bits of it; `lan` gets 256, because that token is one that crosses a
    network somebody else is also on;
  * it binds **loopback** unless `/remote on lan` is typed, and that prints a
    warning naming what is now reachable;
  * every request carries the token, compared with `secrets.compare_digest`;
  * wrong tokens are counted per address, and an address that has sent
    `REMOTE_MAX_BAD_TOKENS` of them is refused for `REMOTE_LOCKOUT` seconds -
    a 128-bit token is not guessable, but a door that lets somebody knock all
    afternoon without anyone hearing it is still the wrong door;
  * **the person is told who is there.** The first request from an address,
    and every attempt with a wrong token, becomes a line at the prompt. On a
    network you share, the useful question is not "could someone get in" but
    "did they", and nothing else here can answer it;
  * the `Host` header has to name this machine, which is what stops a page on
    the internet from walking into `127.0.0.1` through a rebound DNS name;
  * what is mirrored out goes through `vault.redact` first, so a `.env` value
    that is on the terminal because the person ran `!cat .env` is not also on
    the wire.

**What it is not.** This is plain HTTP. On loopback that is the whole story -
the bytes never leave the machine. Over `lan` they cross a network, and anyone
already on that network can read them: the transcript, and the token with it.
So `lan` is for a network you trust, and everything else is `ssh -L`, which is
somebody else's audited code and is why there is no tunnel and no TLS here.

**What the remote sees.** The screen, as text. Everything the harness prints
goes through `sys.stdout`, so mirroring is a tee installed on it rather than a
second rendering that would drift from the first: one transcript, two places,
and no second copy of the formatting to keep in step. ANSI is stripped, a line
being streamed is published as a `tail` before its newline arrives, and the
buffer is a ring (`REMOTE_LINES`) because a session outlives any phone screen.

**What the remote can do.** Type a line - a message or a slash command, both
go into the same loop by the same route - and answer a question. A line typed
remotely marks the turn it starts as *remotely driven*, and that is what
decides where the next approval prompt is asked: the person who started the
work is the person who gets asked to approve it. A question nobody answers
within `REMOTE_ASK_TIMEOUT` is refused, because the safe end of an unanswered
"may I delete this" is no.

Threads: the server is a `ThreadingHTTPServer` in a daemon thread, and every
piece of shared state below is behind one `Condition` - which is also how a
long poll waits without spinning, and how `ask` blocks the main thread until
an answer arrives or the deadline passes.
"""

import collections
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from simple_harness import config
from simple_harness import vault

FORMAT = 1

# Ceiling on one request body. A typed line is a sentence; anything of this
# size is either a mistake or somebody probing.
MAX_BODY = 64 * 1024

# How long a `/state` long poll is held open before answering with whatever it
# has. Under a mobile network's own idle timeout, and short enough that a
# phone that has gone to sleep reconnects rather than hanging.
HOLD_SECONDS = 25.0

# What a browser is allowed to call itself. Anything else is a name that
# resolved to this machine from somewhere it should not have.
_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Everything below is read and written from the server's threads and from the
# main one, so nothing here is touched outside `_wake`.
_wake = threading.Condition()

_server = None
_thread = None
_token = ""
_bound = ()                 # (host, port) actually bound, after the OS chose
_scope = ""                 # "lan" when it was opened to the network
_started = 0.0

_clients = {}               # address -> {"first", "last", "requests"}
_bad = {}                   # address -> {"count", "until"}
_notices = collections.deque(maxlen=50)      # for the prompt to print

_lines = collections.deque(maxlen=1)     # (seq, text); resized at start()
_seq = 0
_tail = ""                  # the line being printed, before its newline
_typed = collections.deque()             # lines from the remote, oldest first
_question = None            # the question waiting for an answer, or None
_busy = False
_driver = "terminal"        # who started the line being worked on
_last_seen = 0.0            # when a remote client last asked for state

_tee = None                 # the stdout wrapper, while one is installed


def _cfg(name, default):
    """A setting, whatever the running config says it is right now."""
    return getattr(config, name, default)


def running() -> bool:
    return _server is not None


def token() -> str:
    return _token


# ---------------------------------------------------------------------------
# the transcript, mirrored off the terminal
# ---------------------------------------------------------------------------

class _Tee:
    """`sys.stdout`, with everything written to it also published.

    A wrapper rather than a `print` of our own: the harness prints from thirty
    places in six modules, and a second path would be a second thing to keep
    in step. Nothing here may raise - this *is* `print` for the rest of the
    program, and a remote that has gone wrong must not be what stops the
    terminal working.
    """

    def __init__(self, target):
        self.target = target

    def write(self, text):
        written = self.target.write(text)
        try:
            publish(text)
        except Exception:
            pass
        return written

    def flush(self):
        try:
            self.target.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self.target.isatty()
        except Exception:
            return False

    def fileno(self):
        return self.target.fileno()

    def __getattr__(self, name):
        return getattr(self.target, name)


def ensure_mirror() -> None:
    """Make sure the tee is the outermost thing on `sys.stdout`.

    Called at the top of every prompt rather than once at start, because
    `prompt_toolkit`'s `patch_stdout` replaces `sys.stdout` for as long as a
    prompt is open and puts the original back afterwards. A tee installed
    *while* a prompt was open would be the thing thrown away, and the mirror
    would stop with nothing to say it had.
    """
    global _tee
    if not running() or isinstance(sys.stdout, _Tee):
        return
    _tee = _Tee(sys.stdout)
    sys.stdout = _tee


def _drop_mirror() -> None:
    global _tee
    if isinstance(sys.stdout, _Tee):
        sys.stdout = sys.stdout.target
    _tee = None


def publish(text: str) -> None:
    """Add what was just printed to the transcript the remote reads.

    Whole lines land in the ring; a line still being streamed is kept as the
    `tail`, so a phone shows an answer arriving rather than a blank screen
    until the paragraph ends. A `\\r` means the terminal was about to overwrite
    what it had - a spinner, a progress line - so only what follows the last
    one survives.
    """
    global _seq, _tail
    if not text:
        return
    with _wake:
        buffer = _tail + text
        parts = buffer.split("\n")
        _tail = parts.pop()
        for line in parts:
            line = line.rsplit("\r", 1)[-1]
            _seq += 1
            _lines.append((_seq, _clean(line)))
        if parts or _tail:
            _wake.notify_all()


def _clean(line: str) -> str:
    """One line as the remote should see it: no ANSI, no secrets."""
    line = _ANSI.sub("", line).replace("\t", "    ").rstrip()
    try:
        return vault.redact(line)
    except Exception:
        return line


def transcript(since: int = 0) -> list:
    """The lines numbered above `since`, oldest first."""
    with _wake:
        return [[seq, text] for seq, text in _lines if seq > since]


# ---------------------------------------------------------------------------
# lines typed on the remote
# ---------------------------------------------------------------------------

def take_line() -> str:
    """The oldest line the remote typed, or "" when there is none.

    Taken once: the caller is the prompt, and a line handed to it twice would
    be a message sent twice.
    """
    with _wake:
        line = _typed.popleft() if _typed else ""
        if line:
            _wake.notify_all()
    return line


def queued() -> int:
    with _wake:
        return len(_typed)


def set_busy(flag: bool) -> None:
    """Whether the harness is working on something, for the remote's badge."""
    global _busy
    with _wake:
        if _busy != bool(flag):
            _busy = bool(flag)
            _wake.notify_all()


def set_driver(who: str) -> None:
    """Who started the line being worked on - `remote` or `terminal`."""
    global _driver
    with _wake:
        _driver = "remote" if who == "remote" else "terminal"


def driven() -> bool:
    """Is a remotely typed line what the harness is working on?"""
    with _wake:
        return _driver == "remote" and _server is not None


# ---------------------------------------------------------------------------
# who is at the door
# ---------------------------------------------------------------------------

def _note(text: str) -> None:
    """Something the person should know, kept for the next free prompt.

    Never printed from here. A handler thread printing would land in the
    middle of a streaming answer, and the channel had this problem first: the
    prompt is where a message from elsewhere belongs, which is where
    `take_notices` is read.
    """
    with _wake:
        _notices.append((time.time(), text))


def take_notices() -> list:
    """Drain what has happened at the door since this was last asked."""
    with _wake:
        found = list(_notices)
        _notices.clear()
    return [text for _, text in found]


def locked_out(who: str) -> float:
    """Seconds this address is still refused for, or 0."""
    with _wake:
        record = _bad.get(who)
        if not record:
            return 0.0
        return max(0.0, record["until"] - time.time())


def note_bad_token(who: str) -> None:
    """Count a wrong token, and shut the address out if there are enough.

    The first one is said out loud, because on a network somebody else is on,
    one wrong token is the only warning there is going to be. The ones after
    it are not: a locked-out script knocking four times a second must not be
    able to fill the terminal with its own noise.
    """
    limit = max(1, int(_cfg("REMOTE_MAX_BAD_TOKENS", 20)))
    for_how_long = max(1.0, float(_cfg("REMOTE_LOCKOUT", 300)))
    with _wake:
        record = _bad.setdefault(who, {"count": 0, "until": 0.0})
        record["count"] += 1
        count = record["count"]
        if count >= limit:
            record["until"] = time.time() + for_how_long
            record["count"] = 0
    if count == 1:
        _note(f"{who} tried the remote with a token that is not this one.")
    elif count >= limit:
        _note(f"{who} has tried {limit} wrong tokens - refused for "
              f"{int(for_how_long)}s. Somebody is guessing; /remote off closes it.")


def note_client(who: str) -> None:
    """A request that got past the token. The first from an address is news."""
    global _last_seen
    with _wake:
        _last_seen = time.time()
        _bad.pop(who, None)          # it is the holder of the link, fumbling
        seen = _clients.get(who)
        if seen:
            seen["last"], seen["requests"] = _last_seen, seen["requests"] + 1
            return
        _clients[who] = {"first": _last_seen, "last": _last_seen, "requests": 1}
    _note(f"{who} opened the remote link.")


def clients() -> list:
    """Who has been through the door, newest first."""
    with _wake:
        return sorted(({"address": who, **rest} for who, rest in _clients.items()),
                      key=lambda row: row["last"], reverse=True)


# ---------------------------------------------------------------------------
# questions, and where they are asked
# ---------------------------------------------------------------------------

def ask(title: str, details, choices, free_text: bool = False,
        timeout: float = 0.0) -> str:
    """Put a question to the remote and wait. "" means nobody answered.

    The empty string is deliberately the same answer as a timeout and as a
    remote that has closed its browser, because every caller treats "no answer"
    as "no": an approval prompt refuses, a plan is not approved. A question
    that expires leaves nothing behind - the next one is asked cleanly.
    """
    global _question
    if not running():
        return ""
    deadline = time.time() + (timeout or float(_cfg("REMOTE_ASK_TIMEOUT", 300)))
    ticket = secrets.token_urlsafe(8)
    with _wake:
        _question = {
            "id": ticket,
            "title": title,
            "details": [[str(label), _clean(str(value))] for label, value in details],
            "choices": [[str(value), str(label)] for value, label in choices],
            "free_text": bool(free_text),
            "asked": time.time(),
            "answer": None,
        }
        _wake.notify_all()
        while _question is not None and _question["id"] == ticket:
            if _question["answer"] is not None:
                answer = _question["answer"]
                _question = None
                _wake.notify_all()
                return answer
            if time.time() >= deadline:
                _question = None
                _wake.notify_all()
                return ""
            # Short waits rather than one long one: on the main thread this is
            # what keeps Ctrl+C able to interrupt a question nobody is going
            # to answer.
            _wake.wait(0.25)
    return ""


def answer(ticket: str, value: str) -> bool:
    """Answer the pending question. False when it is not the one being asked.

    The id is checked rather than assumed: a phone that was showing yesterday's
    question and pressed Allow must not approve today's `rm -rf`.
    """
    with _wake:
        if not _question or _question["id"] != ticket:
            return False
        _question["answer"] = str(value)
        _wake.notify_all()
        return True


def pending() -> dict | None:
    with _wake:
        if not _question:
            return None
        shown = {k: v for k, v in _question.items() if k != "answer"}
        return shown


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------

def _lan_address() -> str:
    """The address this machine has on its own network, or "".

    A UDP socket that is never sent on: connecting one only asks the routing
    table which interface would be used, which is the question being asked.
    """
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        probe.connect(("192.0.2.1", 9))          # TEST-NET-1: routed nowhere
        return probe.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return ""
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(scope: str = "") -> dict:
    """Open the door. Returns what to tell the person; raises on failure.

    `scope` of `lan` binds every interface; anything else binds loopback, and
    loopback is the default because the failure mode of getting this wrong is
    a shell on somebody else's network.
    """
    global _server, _thread, _token, _bound, _scope, _started, _lines, _seq
    if running():
        return status()

    host = "0.0.0.0" if scope == "lan" else str(_cfg("REMOTE_HOST", "127.0.0.1"))
    first = int(_cfg("REMOTE_PORT", 8765))
    with _wake:
        _lines = collections.deque(_lines, maxlen=max(1, int(_cfg("REMOTE_LINES", 500))))
        _seq = _lines[-1][0] if _lines else 0

    server, problem = None, None
    # One port, then the next few: 8765 being busy is routinely this harness's
    # own second window, and refusing over that would be an error about
    # nothing the person did wrong.
    for port in ([first] if first == 0 else range(first, first + 20)):
        try:
            server = _Server((host, port), _Handler)
            break
        except OSError as e:
            problem = e
    if server is None:
        raise RuntimeError(f"could not open a port on {host}: {problem}")

    # Twice the token for the network case. 128 bits is already not guessable,
    # and the extra is not really about guessing: a token that leaves this
    # machine is a token that can be written down, shoulder-read off a screen
    # or left in somebody's browser history, and there is no reason to be
    # stingy with the one that does.
    _token = secrets.token_urlsafe(32 if scope == "lan" else 16)
    _bound = (host, server.server_address[1])
    _scope = "lan" if scope == "lan" else ""
    _started = time.time()
    _server = server
    _thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                               name="simple-harness-remote", daemon=True)
    _thread.start()
    ensure_mirror()
    return status()


def reconfigure() -> dict:
    """Make a setting changed since `start()` true of the door that is open.

    A setting that is changed while the thing it configures is running should
    either take effect or say it has not, and `/set REMOTE_PORT 9000` at a
    prompt with a remote already open is exactly somebody saying "move it".
    So it moves - which means a new token and a dead link, and the caller is
    told so it can print the new one.

    Returns what changed: `{"rebound": (host, port)}` when the address moved,
    `{"resized": n}` when only the transcript did, `{}` when nothing did.
    """
    if not running():
        return {}
    wanted = ("0.0.0.0" if _scope == "lan" else str(_cfg("REMOTE_HOST", "127.0.0.1")),
              int(_cfg("REMOTE_PORT", 8765)))
    # Port 0 means "any", and the OS already answered it: re-reading that as a
    # change would move the door on every unrelated `/set`.
    moved = wanted[0] != _bound[0] or (wanted[1] and wanted[1] != _bound[1])
    if moved:
        scope = _scope
        stop()
        start(scope)
        return {"rebound": _bound}

    kept = max(1, int(_cfg("REMOTE_LINES", 500)))
    if kept != _lines.maxlen:
        _resize(kept)
        return {"resized": kept}
    return {}


def _resize(kept: int) -> None:
    global _lines
    with _wake:
        _lines = collections.deque(_lines, maxlen=kept)


def stop() -> None:
    """Close the door, and forget the token that opened it."""
    global _server, _thread, _token, _bound, _scope, _question, _last_seen
    server, _server = _server, None
    _drop_mirror()
    with _wake:
        _question = None
        _typed.clear()
        _wake.notify_all()
    if server is not None:
        try:
            # From this thread, never from a handler's: `shutdown` waits for
            # the serving loop to notice, and the serving loop is what a
            # handler is running inside.
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
    if _thread is not None:
        _thread.join(timeout=2.0)
    _thread = None
    _token = ""
    _bound = ()
    _scope = ""
    # Who was at the door is forgotten with the door. The addresses were only
    # ever here so the person could be told about them, and a list of who
    # visited outliving the thing they visited is a record nobody asked for.
    # `_last_seen` goes with them, or the next door would open claiming it had
    # just been read by somebody who was at the last one.
    with _wake:
        _clients.clear()
        _bad.clear()
        _last_seen = 0.0


def urls() -> list:
    """Every address this server can be reached at, token included."""
    if not running():
        return []
    host, port = _bound
    names = ["127.0.0.1"] if host in ("0.0.0.0", "127.0.0.1", "") else [host]
    if host == "0.0.0.0":
        lan = _lan_address()
        if lan and lan not in names:
            names.append(lan)
    return [f"http://{name}:{port}/?k={_token}" for name in names]


def status() -> dict:
    """What `/remote` prints, and what the tests read."""
    with _wake:
        return {
            "running": _server is not None,
            "host": _bound[0] if _bound else "",
            "port": _bound[1] if _bound else 0,
            "scope": _scope or "local",
            "token": _token,
            "urls": urls(),
            "clients": len(_clients),
            "refused": sorted(who for who, record in _bad.items()
                              if record["until"] > time.time()),
            "lines": len(_lines),
            "queued": len(_typed),
            "driver": _driver,
            "asking": bool(_question),
            "seen": _last_seen,
            "since": _started,
        }


# ---------------------------------------------------------------------------
# the requests
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "simple-harness"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """Nothing. The terminal belongs to the conversation, not to a log."""

    # -- what every request has to get past ---------------------------------

    def _path_and_query(self):
        path, _, query = self.path.partition("?")
        fields = {}
        for pair in query.split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            fields[key] = _unquote(value)
        return path, fields

    def _authorised(self, fields) -> bool:
        offered = fields.get("k", "")
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            offered = header[7:].strip()
        return bool(_token) and secrets.compare_digest(offered, _token)

    def _host_is_this_machine(self) -> bool:
        """Refuse a `Host` this server was not asked for.

        A token in the URL stops somebody guessing their way in. It does not
        stop a page the person has open from resolving its own name to
        127.0.0.1 and talking to whatever answers - and this answers. What a
        rebound name cannot do is put a plausible `Host` on the request.
        """
        raw = self.headers.get("Host", "")
        name = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
        name = name.strip("[]").lower() or "localhost"
        if name in _LOCAL_NAMES:
            return True
        if _bound and name == str(_bound[0]).lower():
            return True
        # An address is a name nobody had to resolve, so a bare IPv4 that is
        # this machine's own is allowed - that is how a phone reaches it.
        return bool(re.fullmatch(r"[0-9a-f.:]+", name))

    def _reply(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Nothing here is meant to be reachable from another page's script.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8", "replace")
        self._reply(code, body, "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except Exception:
            return {}

    # -- the four things it answers -----------------------------------------

    def _refusal(self, fields):
        """Everything a request has to get past, in the order it has to.

        The `Host` first, because a request from a page that resolved its own
        name here should not even get to spend a guess. Then the lockout, so
        an address that is guessing cannot keep guessing. Then the token.
        Returns `(code, message)` or `None`.
        """
        if not self._host_is_this_machine():
            return 403, "wrong host"
        who = self.client_address[0] if self.client_address else "?"
        waiting = locked_out(who)
        if waiting:
            return 429, f"too many wrong tokens - try again in {int(waiting) + 1}s"
        if not self._authorised(fields):
            note_bad_token(who)
            return 401, "a token is needed"
        note_client(who)
        return None

    def do_GET(self):                                    # noqa: N802
        path, fields = self._path_and_query()
        refused = self._refusal(fields)
        if refused:
            return self._json(refused[0], {"error": refused[1]})

        if path in ("/", "/index.html"):
            page = PAGE.replace("__TOKEN__", _token)
            return self._reply(200, page.encode("utf-8"), "text/html; charset=utf-8")

        if path == "/state":
            try:
                since = int(fields.get("since", "0"))
            except ValueError:
                since = 0
            if fields.get("wait") == "1":
                self._hold(since)
            return self._json(200, _state(since))

        return self._json(404, {"error": "no such thing here"})

    def do_POST(self):                                   # noqa: N802
        path, fields = self._path_and_query()
        refused = self._refusal(fields)
        if refused:
            return self._json(refused[0], {"error": refused[1]})
        payload = self._body()

        if path == "/say":
            text = str(payload.get("text", "")).strip()
            if not text:
                return self._json(400, {"error": "nothing to say"})
            text = config.safe_text(text)[:MAX_BODY]
            with _wake:
                _typed.append(text)
                _wake.notify_all()
            return self._json(200, {"ok": True, "queued": queued()})

        if path == "/answer":
            took = answer(str(payload.get("id", "")), str(payload.get("value", "")))
            return self._json(200 if took else 409,
                              {"ok": took,
                               "error": "" if took else "that question is gone"})

        return self._json(404, {"error": "no such thing here"})

    def _hold(self, since: int) -> None:
        """Wait for something to be worth answering with, or time out.

        A phone asking four times a second is a phone with no battery, and a
        phone asking every thirty is a phone that shows an approval prompt
        half a minute late. So it asks once and this holds the answer.
        """
        deadline = time.time() + HOLD_SECONDS
        with _wake:
            # The question is held by id rather than by whether there is one:
            # one question answered and the next asked between two polls is
            # the case a boolean cannot see, and it is the case where being
            # late matters most.
            was_busy = _busy
            was_asking = _question["id"] if _question else ""
            while (_seq <= since and _busy == was_busy and _server is not None
                   and (_question["id"] if _question else "") == was_asking):
                if time.time() >= deadline:
                    return
                _wake.wait(0.5)


def _state(since: int) -> dict:
    """Everything the page draws itself from, in one answer."""
    with _wake:
        lines = [[seq, text] for seq, text in _lines if seq > since]
        question = None
        if _question:
            question = {k: v for k, v in _question.items() if k != "answer"}
        return {
            "format": FORMAT,
            "seq": _seq,
            "lines": lines,
            "tail": _clean(_tail),
            "busy": _busy,
            "driver": _driver,
            "queued": len(_typed),
            "question": question,
            "title": getattr(config, "SESSION_TITLE", "") or "",
            "model": getattr(config, "MODEL", ""),
            "cwd": os.getcwd(),
        }


def _unquote(text: str) -> str:
    """Percent-decoding, without dragging `urllib` in for one field."""
    from urllib.parse import unquote_plus
    try:
        return unquote_plus(text)
    except Exception:
        return text


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

# One file, no network: a phone on a LAN with no route out still has to be
# able to draw this, so there is nothing to fetch and nothing to fail.
PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>simple-harness</title>
<style>
:root { color-scheme: dark; --bg:#141414; --panel:#1c1c1c; --line:#2c2c2c;
        --text:#e8e4dc; --muted:#8b8b8b; --accent:#d3a04a; --ok:#98c379;
        --warn:#e5b567; --err:#e06c75; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5
       ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
       display:flex; flex-direction:column; height:100dvh; }
header { display:flex; gap:8px; align-items:center; padding:10px 14px;
         border-bottom:1px solid var(--line); background:var(--panel); }
header b { color:var(--accent); font-weight:600; }
header span { color:var(--muted); font-size:12px; overflow:hidden;
              text-overflow:ellipsis; white-space:nowrap; }
#dot { width:8px; height:8px; border-radius:50%; background:var(--muted);
       flex:none; }
#dot.busy { background:var(--warn); animation:pulse 1s infinite; }
#dot.live { background:var(--ok); }
@keyframes pulse { 50% { opacity:.25; } }
#log { flex:1; overflow-y:auto; padding:12px 14px 4px; white-space:pre-wrap;
       overflow-wrap:anywhere; }
#log .t { color:var(--muted); }
#ask { margin:10px 14px; padding:12px; border:1px solid var(--warn);
       border-radius:8px; background:#1f1a12; display:none; }
#ask h3 { margin:0 0 8px; font-size:13px; color:var(--warn); }
#ask dl { margin:0 0 10px; font-size:12px; }
#ask dt { color:var(--muted); }
#ask dd { margin:0 0 6px; overflow-wrap:anywhere; }
#ask .row { display:flex; gap:8px; flex-wrap:wrap; }
button { font:inherit; padding:9px 14px; border-radius:6px; cursor:pointer;
         border:1px solid var(--line); background:#242424; color:var(--text); }
button.go { border-color:var(--ok); color:var(--ok); }
button.no { border-color:var(--err); color:var(--err); }
form { display:flex; gap:8px; padding:10px 14px calc(10px + env(safe-area-inset-bottom));
       border-top:1px solid var(--line); background:var(--panel); }
input { flex:1; min-width:0; font:inherit; padding:11px 12px; border-radius:6px;
        border:1px solid var(--line); background:#111; color:var(--text); }
input:focus { outline:1px solid var(--accent); }
</style></head><body>
<header><i id="dot"></i><b>simple-harness</b><span id="where">connecting…</span></header>
<div id="log"></div>
<div id="ask"><h3></h3><dl></dl><div class="row"></div></div>
<form id="say"><input id="text" placeholder="message, or /command"
  autocomplete="off" autocapitalize="off" autocorrect="off"><button>Send</button></form>
<script>
const KEY = "__TOKEN__";
const log = document.getElementById("log"), ask = document.getElementById("ask");
const dot = document.getElementById("dot"), where = document.getElementById("where");
let seq = 0, tailNode = null, asking = "";

function atBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 60; }
function add(text, cls) {
  const stick = atBottom();
  const div = document.createElement("div");
  if (cls) div.className = cls;
  div.textContent = text === "" ? "\\u00a0" : text;
  log.appendChild(div);
  while (log.childNodes.length > 1200) log.removeChild(log.firstChild);
  if (stick) log.scrollTop = log.scrollHeight;
}

function drawAsk(q) {
  if (!q) { ask.style.display = "none"; asking = ""; return; }
  if (q.id === asking) return;
  asking = q.id;
  ask.querySelector("h3").textContent = q.title;
  const dl = ask.querySelector("dl"); dl.innerHTML = "";
  for (const [label, value] of q.details) {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = label; dd.textContent = value;
    dl.append(dt, dd);
  }
  const row = ask.querySelector(".row"); row.innerHTML = "";
  for (const [value, label] of q.choices) {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = /^(y|1|a)$/.test(value) ? "go" : (/^(n|2)$/.test(value) ? "no" : "");
    b.onclick = () => reply(q.id, value);
    row.appendChild(b);
  }
  if (q.free_text) {
    const b = document.createElement("button");
    b.textContent = "Type an answer…";
    b.onclick = () => { const t = prompt(q.title); if (t) reply(q.id, t); };
    row.appendChild(b);
  }
  ask.style.display = "block";
  ask.scrollIntoView({ block: "nearest" });
}

async function reply(id, value) {
  ask.style.display = "none"; asking = "";
  await fetch("/answer?k=" + KEY, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, value }) });
}

document.getElementById("say").onsubmit = async (e) => {
  e.preventDefault();
  const box = document.getElementById("text"), text = box.value.trim();
  if (!text) return;
  box.value = "";
  add("\\u276f " + text);
  await fetch("/say?k=" + KEY, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }) });
};

async function poll() {
  for (;;) {
    try {
      const r = await fetch("/state?k=" + KEY + "&since=" + seq + "&wait=1");
      if (r.status === 401) { where.textContent = "this link is no longer valid"; return; }
      const s = await r.json();
      dot.className = s.busy ? "busy" : "live";
      where.textContent = (s.title || s.cwd) + "  ·  " + s.model;
      for (const [n, text] of s.lines) { add(text); seq = n; }
      if (tailNode) { tailNode.remove(); tailNode = null; }
      if (s.tail) {
        const stick = atBottom();
        tailNode = document.createElement("div");
        tailNode.className = "t"; tailNode.textContent = s.tail;
        log.appendChild(tailNode);
        if (stick) log.scrollTop = log.scrollHeight;
      }
      drawAsk(s.question);
    } catch (err) {
      dot.className = ""; where.textContent = "reconnecting…";
      await new Promise(r => setTimeout(r, 2000));
    }
  }
}
poll();
</script></body></html>
"""

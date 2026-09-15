# Changelog

What changed, and why it was worth changing. Versions follow [semantic
versioning](https://semver.org), with the qualification every 0.x project owes
its users: **while the major version is 0, the surface named in
[Compatibility](#compatibility) can still move.** It is written down and pinned
by a test from 0.6.0 onwards so that by 1.0.0 the promise is one this project
has already been keeping for a while, rather than one it makes on the day.

## Compatibility

Five things are the contract. `tests/test_compat.py` fails when one of them
disappears, so removing or renaming any of it takes a deliberate edit to that
file - which is the point.

| Surface | What is promised |
|:---|:---|
| Slash commands | A command that exists keeps its name and keeps meaning what it meant |
| `/set` settings | A setting name in `config.py` is public the moment it exists, because `/set` derives its list from there |
| Tool names | Model-facing, and written into saved sessions - a rename breaks replay, not just a prompt |
| State layout | `~/.localchat/`: `sessions/`, `memory.json`, `settings.json`, `permissions.json`, `mcp.json`, `history`, `skills/` - and `LOCALCHAT_HOME` to move all of them |
| Project files | `.permissions.json`, `.mcp.json`, `skills/<name>/SKILL.md`, read from the working directory first |

Not promised: anything inside `simple_harness.*`. The modules are an
implementation, not an API, and the reusable pieces are meant to leave for
packages of their own rather than be imported from here.

---

## Unreleased

### Remote control: one door into the session that is already running

A harness is a terminal, and a terminal is somewhere you have to be. The moment
a request takes minutes rather than seconds - a `/deepthink` pass, a suite the
model is chasing - the two things you need are *what is it doing* and *yes, go
ahead*, and both are behind a keyboard you have walked away from. Worse than
slow: a turn that stops at `Allow? [y/n]` on a screen nobody is looking at has
hung, and nothing says so.

`/remote on` prints a link. Open it on a phone and you are at the prompt - the
transcript as it is printed, a box that types into the same loop the keyboard
types into, and the approval prompts themselves, with buttons.

The rule that makes it a control rather than a viewer: **a question is asked
wherever the person driving the turn is.** A line typed on the phone marks the
turn, and every blocking question in the harness - the approval prompt,
`get_input`, `submit_plan_for_approval` - now goes through one place that knows
which that is. Both are still printed on the terminal, so the person at the
desk can read what was asked and what came back. Nobody answering inside
`REMOTE_ASK_TIMEOUT` is a no.

What is behind the link is a shell, so: off until `/remote on`; loopback unless
`/remote on lan`, which says what it is doing in as many words; a token made
when the door opens, printed once, never written to disk and gone when it
closes - 128 bits on loopback, 256 for `lan`, which is the one that crosses a
network somebody else is also on; a `Host` that is not this machine refused
before the token is read; wrong tokens counted per address and shut out after
`REMOTE_MAX_BAD_TOKENS` of them; and the mirrored transcript redacted the way
the model's copy is, so a `.env` value that is on your screen because *you* ran
`!cat .env` does not go out over the wire.

And you are told who is there. The first request from an address, and the first
wrong token from one, arrive at your prompt the way another agent's message
does - `◆ 192.168.0.14 opened the remote link.` On a shared network the
question worth answering is not whether somebody *could* get in but whether
they did, and nothing else here can answer it.

**Over a network the link is not enough on its own.** A browser that arrives
over `lan` is shown a box rather than the transcript: six digits, printed in the
terminal the harness runs in, good for two minutes and three guesses. Type them
on the phone and it gets a session of its own; anything else stays outside.
That is a second factor rather than a second copy of the first - the link
crosses the network and can be photographed, read aloud or left in a history,
and the terminal cannot. `REMOTE_PAIR` chooses when it is asked (`lan`,
`always`, `never`) and `/remote forget` drops every browser that has paired.

**`/remote qr`** draws the link as something to point a camera at, because
nobody types forty-three random characters into a phone twice. Black modules on
a white ground the harness paints itself, so it scans in any terminal theme.
There is no library behind it: `qr.py` is a byte-mode encoder in the stdlib,
level M, versions 1 to 9. `tests/test_qr.py` reads each symbol back the way a
scanner does - the mask out of its own format bits, the zigzag, the blocks - and
checks that every block still satisfies its Reed-Solomon parity, which is one
check over the format bits, the placement, the block tables, the interleaving
and the arithmetic at once.

It is plain HTTP, which on loopback is the whole story and over `lan` is a
network you are choosing to trust. There is deliberately no TLS and no account:
from anywhere else, forward the port over `ssh -L`.

`REMOTE_PORT` and `REMOTE_HOST` are ordinary settings, and `/set REMOTE_PORT
9000` at a prompt with a remote already open *moves* it - new token, new link,
printed on the spot - rather than waiting for a restart.

### Fixed, from the first afternoon of it running on Windows

- **A message that arrived while you were at the prompt lost its colours** and
  arrived as `?[38;2;250;189;47m◆ …` instead. Printing above a live prompt goes
  through prompt_toolkit's own console writer on Windows, which hands escape
  sequences to the console as characters; they are handed over as `ANSI(...)`
  now. The agent channel's messages had the same fault and the same fix.
- **Opening the link reported you at your own prompt as an intruder** - twice,
  once for the tab icon and once for the page. A browser fetches `/favicon.ico`
  and friends by itself, without the token; those paths answer 404 and are
  counted as nothing.
- **A phone that locked its screen printed a stack trace** into the middle of
  the conversation: `socketserver` reports a handler's exception that way, and
  a dropped long poll is `ConnectionAbortedError` on Windows. A socket giving
  way is now the ordinary end of a request, and anything that is not one is a
  single line at the prompt.
- **The notice marker was a glyph Windows Terminal cannot draw.** U+26BF, the
  "squared key", is not in its default font and came out as a box. It is `◆`
  now, from the Geometric Shapes block everything else in this interface uses.
- **`/model` from the phone asked the terminal.** `connect` now asks through
  the same place every other blocking question does, and passes its numbered
  list along as buttons. An API key is the deliberate exception: it is not
  typed over plain HTTP, whoever is driving.
- **The page now knows what may be typed into it.** `/` lists the slash
  commands with what each does - the table `/help` renders, served as
  `/commands` - and tapping one inserts it. `!` turns the box amber and says
  it runs on that machine as you, which is the warning the terminal has had
  over its own prompt since the shell escape existed.
- **Redaction was silent about itself.** A `.env` value that is also an
  ordinary word - `PROJECT_DIR=simple_harness` - is a secret by the only rule
  that never lets a key through, so `!dir` came back full of
  `{{env:PROJECT_DIR}}` with nothing to say why. A `!` command whose output was
  redacted now names what was hidden. README §13a states the three conditions
  outright.

The transcript is a tee on `sys.stdout` rather than a second rendering, which is
why what the phone shows is exactly what the terminal shows, tool boxes and all.

New: `/remote` (with `qr` and `forget`), `remote.py`, `qr.py`,
`tests/test_remote.py`, `tests/test_qr.py`, and `REMOTE_ENABLED`, `REMOTE_HOST`,
`REMOTE_PORT`, `REMOTE_LINES`, `REMOTE_ASK_TIMEOUT`, `REMOTE_PAIR`,
`REMOTE_MAX_BAD_TOKENS`, `REMOTE_LOCKOUT`.

---

## 0.6.0 - 2026-09-08

The release where the harness stopped trusting the model's account of its own
work, in four different places.

### Auto-verify: the project's own check runs itself

A model that edits a file says "done" without running anything - not from
laziness, but because at 4B the thought does not occur, and a system prompt
telling it to check its work is forgotten by the third tool call. So the
harness runs the check instead. After a turn that wrote a file - once per turn,
not once per file - the project's own suite runs and a failure is put back in
front of the model to fix.

The check is found, never invented: a marker file (`pyproject.toml`,
`package.json`, `Cargo.toml`, `go.mod`), an installed runner, and for npm a
`test` script that is not the placeholder `npm init` writes. **What runs is
decided by the extension of what changed**, so a repository holding both
`pyproject.toml` and `package.json` still sends `.py` to pytest and `.ts` to
npm. It gives up rather than nagging: a suite that exceeds `VERIFY_TIMEOUT`
switches itself off, and three failures in a row stop the loop and ask for an
explanation instead of a fourth guess. Python failures come back with
`--showlocals`, so the model is handed the state that caused the failure rather
than only the line it happened on. The same failure arriving twice is counted
as one attempt going nowhere.

`/autoverify on|off`, `VERIFY_TIMEOUT`, `VERIFY_OUTPUT_CHARS`.

### `/tdd`: "make the test pass" cannot mean "edit the test"

`/tdd <request>` locks this project's test files for one request. The lock
lifts itself when the turn ends. Without it, the shortest path from a red test
to a green one runs straight through the assertion.

### Deepthink can start itself over

Stage 6 was the only stage asked to find the work wanting, and it had nowhere
to put what it found: a verify that saw half the plan undone ended the chain
and handed that report back as the answer. Now it says so - `MORE_WORK_NEEDED`,
or its own report read back in one short call - and the six stages run again
**from stage 1**, because what is left after a failed pass is a different piece
of work and planning it is the step that would otherwise be skipped.

Bounded three ways: `DEEPTHINK_MAX_PASSES` (3) is the ceiling, the next pass is
told to finish what the report named and not to widen it, and a pass with
nothing left in it ends after one turn on the existing `NO_PLAN_NEEDED` path.
The two gates lean opposite ways on purpose - unclear means "there is work" when
deciding whether to build, and "it is finished" when deciding whether to go
round again, because a chain that restarts itself on a maybe does not terminate.

### `edit_file` reads what the model meant, and refuses the rest with evidence

An anchor whose spelling can only mean one thing is repaired rather than
rejected. Everything else is refused **with the real lines attached**, so the
next attempt is made against the file instead of against memory. Hashes gained a
third character, the quoted line beats the hash when the two disagree, and a
successful edit hands back the lines around it - so the following edit needs no
re-read.

### A big MCP server is announced, not described

One `@playwright/mcp` server cost 3,549 prompt tokens (4,637 as a native
`tools` field) on every request, in conversations that had nothing to do with a
browser. A server with `MCP_LAZY_MIN_TOOLS` (6) tools or more now sends its name
and its tool names only - 186 tokens - and `use_mcp_server` sends the schemas
when the model asks for them. This is not a permission: calling a tool on a
server that was never described still works, and the call is what loads it.

### An agent that was asked a question is made to answer it

Two 4B instances in one project negotiated over a claimed file correctly right
up to the last step, where the reply was written into the answer - addressed to
the other agent, delivered to nobody - while the other agent sat waiting. The
channel now records who addressed this agent directly, `send_agent_message`
clears it, and a turn that ends with the question outstanding is told so once.
Once, not until it complies: a model that ignores the second reminder ignores
the fourth.

### Also

- README gained a comparison against other harnesses, with the claims that are
  actually enforced marked as such.
- A version already on the index no longer fails the publish workflow, so the
  hand-run `testpypi` → `pypi` → GitHub Release order stops tripping over
  itself.

---

## 0.5.0 - 2026-09-05

- **A stateful Python VM** (`run_python`): a scratch process that keeps its
  variables between calls, with ceilings on memory, output, file size and time,
  and a restart when it dies.
- **Runtime settings** (`/set`): what is settable is *derived* from `config.py`
  rather than listed, so a setting is settable the moment it exists and there is
  no second table to drift. Only the deviations are written to
  `~/.localchat/settings.json`, so an improved default still reaches anyone who
  never overrode it.
- **Prompt caching** for the hosted providers, with a test that fails when
  nothing is being read from the cache - a cache that silently stops working is
  worse than none, because the bill is the only place it shows.
- `/usage` stopped calling two different counts "turns" and now shows the fixed
  per-request cost separately.

## 0.4.2 - 2026-09-03

- **`@path` mentions**: a file, or a directory listing, attached to the message
  by typing `@` - completed from what is actually on disk, and behaving the same
  on Windows.
- **`!command`** at the prompt, and **CLI session resume**.
- CI now catches a tag that disagrees with `simple_harness.__version__`, before
  the upload that cannot be taken back.

## 0.3.0 - 2026-09-02

- **Deepthink separated finding faults from fixing them.** A stage allowed to
  fix stops looking as soon as it has something to fix, so the second half of
  its own list went unread. Review is read-only and writes a numbered list;
  revise turns the tools back on and works through it.
- **What this does to the machine is said once, before it does it** (`terms.py`).
- A failed tool result names the call that failed, and long errors are trimmed
  from the middle rather than the end.
- Tool results are trimmed only when the context budget actually needs it.

## 0.2.0 - 2026-09-01

- **`~/.localchat`**: sessions, memory, history and saved keys moved out of
  whatever directory the harness happened to start in. What stays per-project is
  what is genuinely about the project - `.permissions.json`, `.mcp.json`,
  `skills/`.
- **Installable**, Apache-2.0, named Simple Harness, published through Trusted
  Publishing with a dry-run target.
- `--help` and `--version`; the version derives from the package, so there is
  one copy of it.

## Before that

The first tagged release is `v0.2.0`; everything earlier is in the git history.
That history is where the harness got its shape: the tool registry that renders
the prompt and binds dispatch from one table, the JSON repair engine and raw
`<content>` blocks that took local-model session failures from 7/7 to 0/3,
native function calling decided per model rather than per provider, a git commit
per AI edit with `/undo`, sub-agents, MCP without an SDK, and deepthink itself.

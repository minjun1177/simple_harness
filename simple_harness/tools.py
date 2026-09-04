import os
import re
import json
import subprocess
import time
import shutil
import hashlib
import inspect
import requests
import psutil
from simple_harness import config
from simple_harness.config import S, TREE_SITTER_AVAILABLE, _TS_LANGUAGES, _EXT_TO_LANG
from simple_harness.tui import _fmt_tool_call, _approval_prompt
from simple_harness.skills import handle_use_skill
from simple_harness import mcp_client
from simple_harness import channel
from simple_harness import git_ops
from simple_harness import permissions
from simple_harness import shell_session
from simple_harness import toolspec
from simple_harness import vm
from simple_harness.websearch import search_web as search_pipeline, strip_html

if TREE_SITTER_AVAILABLE:
    from simple_harness.config import Parser, Query, QueryCursor


def handle_search_web(query: str) -> str:
    return search_pipeline(query)


def _trim_output(text: str) -> str:
    limit = config.CMD_OUTPUT_CHARS
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return ("...[earlier output trimmed]\n" + text[-limit:])


def _session_result(session, output: str, ended: bool, timed_out: bool) -> str:
    """Turn one read from a live command into the text the model sees."""
    output = _trim_output(output)

    if timed_out:
        session.close()
        return (f"[Error] '{session.command}' produced output for {config.CMD_TIMEOUT}s "
                "without stopping, so it was killed. Do not run it again unless you "
                "narrow it down."
                + (f"\n\n{output}" if output else ""))

    if ended:
        code = session.close()
        if code:
            return f"[Error] Command failed (exit code {code}).\n{output}" if output \
                else f"[Error] Command failed (exit code {code})."
        return output or "(the command produced no output)"

    # Still running and gone quiet: from out here that is what a program sitting
    # at a prompt looks like.
    shell_session.register(session)
    return (f"{output}\n\n"
            f"[Waiting] '{session.command}' is still running and has printed nothing for "
            f"{config.CMD_IDLE_TIMEOUT:g}s, so it is most likely waiting for input. "
            f"Read the output above and answer it with send_input:\n"
            f'    {{"name": "send_input", "arguments": {{"session": "{session.id}"}}}}\n'
            "    <stdin>\n    <your answer>\n    </stdin>\n"
            "Send an empty <stdin> block to just wait for more output, or call "
            f'end_process with session "{session.id}" to stop it.').strip()


def safe_run_cmd(command_string: str, stdin_text: str = "") -> str:
    """Run a shell command, and stay connected to it while it runs.

    The command is handed to the shell as written. It used to be split with
    `shlex` and then passed to `shell=True`, which mangled Windows paths (shlex
    eats backslashes) and left the shell to re-quote a list it had never been
    given - so `run_cmd` and what actually ran could differ.

    The process is not waited on to completion. Its output is drained as it
    appears, and when it goes quiet the output so far is returned along with the
    session id, so the model can read the prompt and answer it with `send_input`.
    That is the only way an interactive program can work here: its prompts are
    captured, so the user never sees them, and letting it hold the terminal just
    froze the app with nothing on screen.
    """
    command = (command_string or "").strip()
    if not command:
        return "[Error] Empty command."

    stdin_text = stdin_text if isinstance(stdin_text, str) else ""

    details = [("command", command)]
    if stdin_text.strip():
        preview = stdin_text if len(stdin_text) <= 200 else stdin_text[:200] + "...[truncated]"
        details.append(("input", preview.replace("\n", " ⏎ ").strip()))
    if not _approval_prompt("Run Command", details, rule=f"run_cmd({command})"):
        return "[System] User denied command execution."

    try:
        session = shell_session.start(command)
    except Exception as e:
        return f"[Error] Failed to execute command: {e}"

    if stdin_text.strip():
        session.send(stdin_text)

    try:
        output, ended, timed_out = session.read_until_idle(
            config.CMD_IDLE_TIMEOUT, time.time() + config.CMD_TIMEOUT)
    except KeyboardInterrupt:
        session.close()
        return "[System] The command was cancelled by the user."

    return _session_result(session, output, ended, timed_out)


def handle_send_input(session_id: str, text: str) -> str:
    """Answer a running command's prompt and read what it says next."""
    shell_session.prune()
    session = shell_session.get(session_id)
    if session is None:
        running = shell_session.active()
        if not running:
            return ("[Error] No command is waiting for input. Start one with run_cmd "
                    "first - a command that has already finished cannot be resumed.")
        listing = ", ".join(f"'{s.id}' ({s.command})" for s in running)
        return f"[Error] No session '{session_id}'. Currently waiting: {listing}"

    text = text if isinstance(text, str) else str(text or "")
    details = [("session", f"{session.id}  ({session.command})"),
               ("input", text.strip().replace("\n", " ⏎ ") or "(nothing - just wait)")]
    if not _approval_prompt("Send Input", details, rule=f"send_input({session.command})"):
        return "[System] User denied sending input."

    if text.strip() and not session.send(text):
        code = session.close()
        return (f"[Error] '{session.command}' had already exited (code {code}), "
                "so the input went nowhere.")

    try:
        output, ended, timed_out = session.read_until_idle(
            config.CMD_IDLE_TIMEOUT, time.time() + config.CMD_TIMEOUT)
    except KeyboardInterrupt:
        session.close()
        return "[System] The command was cancelled by the user."

    return _session_result(session, output, ended, timed_out)


def handle_end_process(session_id: str) -> str:
    session = shell_session.get(session_id)
    if session is None:
        return f"[Error] No running session '{session_id}'."
    command, sid = session.command, session.id
    session.close()
    return f"[Success] Stopped '{command}' (session {sid})."


_HASHLINE_PATTERN = re.compile(r'^\d+:[0-9a-f]{2}\|')

def _line_hash(line: str) -> str:
    return hashlib.md5(line.encode("utf-8")).hexdigest()[:2]

def _encode_hashlines(content: str) -> str:
    lines = content.split("\n")
    result = []
    for i, line in enumerate(lines, 1):
        h = _line_hash(line)
        result.append(f"{i}:{h}|{line}")
    return "\n".join(result)

def _strip_hashlines(content: str) -> str:
    lines = content.split("\n")
    if not lines:
        return content

    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return content
    matched = sum(1 for l in non_empty if _HASHLINE_PATTERN.match(l))
    ratio = matched / len(non_empty)

    if ratio < 0.8:
        return content

    stripped = []
    for line in lines:
        m = _HASHLINE_PATTERN.match(line)
        if m:
            stripped.append(line[m.end():])
        else:
            stripped.append(line)
    return "\n".join(stripped)


# ---------------------------------------------------------------------------
# run_python: the scratch VM (see vm.py for why it is not just `run_cmd python`)
# ---------------------------------------------------------------------------

_VM_NAMES_SHOWN = 12


def _trim_both_ends(text: str, limit: int) -> str:
    """Trim from the middle. The first lines and the last lines both matter.

    `_trim_output` keeps the tail, which is right for a command still printing.
    Here the whole run is over, and a loop that printed ten thousand lines is
    understood from how it started and how it ended - invariant 5.6b, applied
    to a tool result rather than to the context budget.
    """
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return (text[:head] + f"\n...[{len(text) - limit} characters trimmed]...\n"
            + text[-tail:])


def _truthy(value) -> bool:
    """`reset` arrives as a real boolean, or as the word for one.

    It has to be read carefully rather than with `bool()`: a model that sends
    `"reset": "false"` means the opposite of what the string is worth, and
    getting that wrong throws away every variable the model was working with.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "on")
    return bool(value)


def _vm_footer(reply: dict) -> str:
    """What the model most needs to know afterwards: what it still has."""
    new_names = [name for name in (reply.get("new") or []) if name]
    if not new_names:
        return ""
    shown = ", ".join(new_names[:_VM_NAMES_SHOWN])
    if len(new_names) > _VM_NAMES_SHOWN:
        shown += f", and {len(new_names) - _VM_NAMES_SHOWN} more"
    return f"\n\n(kept for your next run_python call: {shown})"


def handle_run_python(content: str, stdin: str = "", reset: bool = False) -> str:
    """Run a snippet in the scratch VM and say what came back."""
    code = _strip_hashlines(content if isinstance(content, str) else "")
    stdin_text = stdin if isinstance(stdin, str) else ""

    if not code.strip():
        return ("[Error] The call carried no code, so nothing was run. Send "
                "run_python again with the Python in a <content> block right "
                "after the JSON object.")

    lines = code.strip().splitlines()
    preview = "\n".join(lines[:6]) + ("\n..." if len(lines) > 6 else "")
    details = [("code", preview), ("in", vm.scratch_dir())]
    if stdin_text.strip():
        details.append(("input", stdin_text.replace("\n", " ⏎ ").strip()))
    if not _approval_prompt("Run Python", details, rule="run_python"):
        return "[System] User denied running the code."

    reply = vm.run(code, stdin_text, _truthy(reset))
    printed = _trim_both_ends(reply.get("output") or "", config.VM_OUTPUT_CHARS)

    # The VM was stopped, so the namespace is gone. Say that outright: a model
    # told only "it failed" goes on referring to a variable that no longer
    # exists, and the next error is a NameError that explains nothing.
    if reply.get("timeout"):
        return (f"[Error] The code was still running after {reply['timeout']:g}s, so the "
                "Python VM was stopped and restarted - every variable from earlier "
                "calls is gone. Look for a loop that never ends, or split the work up."
                + (f"\n\nIt printed this first:\n{printed}" if printed.strip() else ""))
    if reply.get("crashed"):
        detail = reply.get("detail") or ""
        return (f"[Error] {reply['crashed']}. It has been restarted, so every variable "
                "from earlier calls is gone."
                + (f"\n{detail}" if detail else "")
                + (f"\n\nIt printed this first:\n{printed}" if printed.strip() else ""))

    if reply.get("error"):
        head = f"It printed this before it failed:\n{printed}\n\n" if printed.strip() else ""
        return f"[Error] {head}{reply['error']}"

    parts = [f"[Success] ran in {reply.get('seconds', 0.0):.2f}s."]
    if printed.strip():
        parts.append(printed.rstrip())
    if reply.get("value") is not None:
        parts.append(f"=> {reply['value']}")
    if len(parts) == 1 and not reply.get("new"):
        parts.append("The code ran and produced nothing. To see a value, print() it "
                     "or end the snippet with the expression on its own line.")
    return "\n\n".join(parts) + _vm_footer(reply)




# ---------------------------------------------------------------------------
# editing by hashline anchor
# ---------------------------------------------------------------------------
#
# `read_file` returns every line as `50:1f|print(answer)`, and until now that
# prefix was only a display aid: `edit_file` stripped it off and matched what
# was left as text. So the model still had to reproduce the line exactly - every
# space of indentation, every quote, every backslash - which is the single thing
# a small model gets wrong most often. And a line that appears twice anywhere in
# the file could not be edited at all: the snippet was ambiguous, and the edit
# was refused rather than guessed at.
#
# The prefix is enough on its own. `50` says which line to replace, and `1f`
# proves the model is looking at the version of it that is on disk right now.
# So an `old_content` made only of prefixes names its lines directly and the
# text is never retyped - and because a line number is unique, "found twice"
# stops being a possible answer.
#
# The hash is what makes this safe rather than merely convenient. A line number
# on its own would happily point at whatever has since moved into that position;
# with the hash, an edit against a stale reading of the file is refused and the
# model is told what is actually there now.

# `50:1f`, or the whole row as read_file printed it: `50:1f|print(answer)`.
_ANCHOR = re.compile(r'^\s*(\d+)\s*:\s*([0-9a-fA-F]{2})\s*(?:\|(.*))?$')
# A span, both ends verified: `50:1f-53:9c`.
_ANCHOR_SPAN = re.compile(r'^\s*(\d+)\s*:\s*([0-9a-fA-F]{2})\s*-\s*'
                          r'(\d+)\s*:\s*([0-9a-fA-F]{2})\s*$')


def _parse_anchors(old_content: str):
    """The lines an `old_content` *names*, or None if it quotes them instead.

    Returns `(first, last, checks, spanned)`. `checks` is the (line, hash, text)
    triples to verify - for a span only the two ends, since nobody can be asked
    to list every line between them.

    One ordinary line anywhere in the snippet makes the whole thing text: this
    has to be certain before it takes over, because falling through to the old
    behaviour is always safe and taking over wrongly is not.
    """
    rows = [row for row in (old_content or "").split("\n") if row.strip()]
    if not rows:
        return None

    if len(rows) == 1:
        span = _ANCHOR_SPAN.match(rows[0])
        if span:
            first, first_hash, last, last_hash = span.groups()
            return (int(first), int(last),
                    [(int(first), first_hash.lower(), None),
                     (int(last), last_hash.lower(), None)], True)

    checks = []
    for row in rows:
        one = _ANCHOR.match(row)
        if not one:
            return None
        number, digest, text = one.groups()
        checks.append((int(number), digest.lower(), text))
    return (checks[0][0], checks[-1][0], checks, False)


def _anchor_target(filepath: str, file_content: str, old_content: str) -> tuple:
    """Resolve `old_content` to a span of lines.

    Three outcomes, and the caller needs to tell them apart:

        (None, None, "")       not anchors at all - match it as text, as before
        (None, None, <error>)  anchors, but they do not describe the file
        (start, end, "")       these lines, verified against what is on disk
    """
    parsed = _parse_anchors(old_content)
    if parsed is None:
        return None, None, ""
    first, last, checks, spanned = parsed
    lines = file_content.split("\n")

    if last < first:
        return None, None, (f"[Error] The anchors run backwards: {first} comes after "
                            f"{last}. Name the first line of the span first.")
    out_of_range = next((n for n, _, _ in checks if n < 1 or n > len(lines)), 0)
    if out_of_range:
        return None, None, (f"[Error] There is no line {out_of_range} in {filepath} - "
                            f"it has {len(lines)} lines. read_file it again and take "
                            f"the anchors from that listing.")
    if not spanned:
        numbers = [n for n, _, _ in checks]
        if numbers != list(range(first, last + 1)):
            return None, None, (
                f"[Error] The anchors {', '.join(str(n) for n in numbers)} are not one "
                f"unbroken run of lines. Either list every line from {first} to {last}, "
                f"or write the span as {first}:{checks[0][1]}-{last}:{checks[-1][1]}.")

    for number, digest, text in checks:
        line = lines[number - 1]
        if _line_hash(line) == digest:
            continue
        # The hash is two hand-copied characters and the text beside it is not,
        # so a line that reads exactly right is taken as the stronger evidence.
        # Trailing whitespace is invisible and routinely dropped in copying;
        # leading whitespace is the indentation and has to match.
        if text is not None and text.rstrip() == line.rstrip():
            continue
        return None, None, (
            f"[Error] Line {number} of {filepath} is not what {number}:{digest} says it "
            f"is. It now reads {number}:{_line_hash(line)}|{line[:120]}\n"
            f"The file has changed since you read it, or the anchor was mistyped. "
            f"read_file it again and use the anchors from the new listing.")

    return first, last, ""


# The same row `read_file` printed, handed back with different text after the
# `|`: `38:ff|print()` means "line 38, which currently hashes to ff, becomes
# print()". One row says which line, proves it is the line that was read, and
# carries the replacement - so the shortest possible edit is one line long and
# the old text is never repeated anywhere.
#
# Leading whitespace is allowed and dropped. `read_file` never indents a row, so
# an indented one is a model that reformatted the block - and writing its literal
# text into the file, which is what would otherwise happen, is a silent wrong
# edit. A real source line that looks like `50:1f|...` is imaginable; one that
# still looks like it after this much of a coincidence is not.
_ANCHORED_LINE = re.compile(r'^\s*(\d+)\s*:\s*([0-9a-fA-F]{2})\|(.*)$')


def _parse_patch(new_content: str):
    """`new_content` read as rows that each replace the line they name, or None.

    Every row has to be one, blank rows included - a `new_content` that is only
    partly anchored is ordinary replacement text and is left alone.
    """
    rows = (new_content or "").split("\n")
    if not any(row.strip() for row in rows):
        return None
    patch = []
    for row in rows:
        matched = _ANCHORED_LINE.match(row)
        if not matched:
            return None
        number, digest, text = matched.groups()
        patch.append((int(number), digest.lower(), text))
    return patch


def _patch_disagrees(old_content: str, patch: list) -> str:
    """Whether an `old_content` was given that says something else.

    A patch already names its lines, so `old_content` has nothing left to add:
    empty is the expected case and the same anchors are accepted. Anything else
    is two different instructions in one call, and picking one of them silently
    is how the wrong line gets edited.
    """
    if not (old_content or "").strip():
        return ""
    parsed = _parse_anchors(old_content)
    if parsed is not None:
        first, last, checks, spanned = parsed
        named = list(range(first, last + 1)) if spanned else [n for n, _, _ in checks]
        if sorted(named) == sorted(number for number, _, _ in patch):
            return ""
    lines = ", ".join(str(number) for number, _, _ in patch)
    return (f"[Error] old_content and new_content do not name the same lines. When "
            f"each new line carries its own anchor, new_content already says "
            f"everything - it is replacing line(s) {lines}. Send it with old_content "
            f"empty, or with exactly those anchors in it.")


def _edit_by_patch(filepath: str, file_content: str, patch: list,
                   old_content: str) -> str:
    """Apply `38:ff|print()` rows: each replaces the one line it names."""
    problem = _patch_disagrees(old_content, patch)
    if problem:
        return problem

    lines = file_content.split("\n")
    seen = set()
    for number, digest, _ in patch:
        if number in seen:
            return (f"[Error] Line {number} is given twice in new_content. A line can "
                    f"only become one thing. To replace one line with several, put "
                    f"the anchor in old_content and the new lines, unanchored, in "
                    f"new_content.")
        seen.add(number)
        if number < 1 or number > len(lines):
            return (f"[Error] There is no line {number} in {filepath} - it has "
                    f"{len(lines)} lines. read_file it again and take the anchors "
                    f"from that listing.")
        current = lines[number - 1]
        if _line_hash(current) != digest:
            # There is no second piece of evidence here: the text beside the
            # anchor is what the line is to become, not what it is now. So the
            # hash is the whole check, and it is not waived.
            return (f"[Error] Line {number} of {filepath} is not what {number}:{digest} "
                    f"says it is. It now reads {number}:{_line_hash(current)}|"
                    f"{current[:120]}\nWriting a new line over it would overwrite "
                    f"something you have not read. Nothing was written - read_file "
                    f"again and take the anchors from the new listing.")

    details = [("path", filepath)]
    for number, _, text in patch[:8]:
        details.append((f"line {number}",
                        f"{_preview(lines[number - 1], 60)}  →  {_preview(text, 60)}"))
    if len(patch) > 8:
        details.append(("", f"...and {len(patch) - 8} more"))
    if not _approval_prompt("Edit File", details, rule=f"edit_file({filepath})"):
        return "[System] User denied file edit."

    for number, _, text in patch:
        lines[number - 1] = text
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        return f"[Error] Cannot write file: {e}"

    which = ", ".join(str(number) for number, _, _ in patch)
    return (f"[Success] File edited: {filepath} (line{'s' if len(patch) > 1 else ''} "
            f"{which} replaced). One line became one line, so nothing below moved "
            f"and the rest of your anchors are still good.")


def _preview(text: str, limit: int = 150) -> str:
    return text[:limit] + ("..." if len(text) > limit else "")


def _edit_by_anchor(filepath: str, file_content: str, start: int, end: int,
                    new_content: str) -> str:
    """Replace lines `start`..`end` outright. Every anchor has been verified."""
    lines = file_content.split("\n")
    replaced = "\n".join(lines[start - 1:end])
    count = end - start + 1
    where = f"line {start}" if count == 1 else f"lines {start}-{end} ({count} lines)"

    # An empty replacement removes the lines rather than leaving them blank,
    # which is the same thing `edit_file` has always done when asked to replace
    # a snippet with nothing.
    replacement = new_content.split("\n") if new_content else []

    details = [("path", filepath), ("replacing", where),
               ("from", _preview(replaced)),
               ("to", _preview(new_content) if replacement else "(the lines are removed)")]
    if not _approval_prompt("Edit File", details, rule=f"edit_file({filepath})"):
        return "[System] User denied file edit."

    lines[start - 1:end] = replacement
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        return f"[Error] Cannot write file: {e}"

    moved = ("" if len(replacement) == count else
             " Every line below it has moved, so read_file again before anchoring "
             "another edit to this file.")
    return f"[Success] File edited: {filepath} ({where} replaced).{moved}"


def handle_read_file(filepath: str) -> str:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        if not config.RETURN_ALL_FILE_CONTENT and len(content) > config.FILE_MAX_DISPLAY_LENGTH:
            content = content[:config.FILE_MAX_DISPLAY_LENGTH] + "\n...[Too long]..."
        return _encode_hashlines(content)
    except Exception as e:
        return f"[Error] Cannot read file: {e}"

def handle_write_file(filepath: str, content: str) -> str:
    if not filepath:
        return ("[Error] No 'filepath' was given. Send it inside \"arguments\" in the "
                "tool call JSON.")

    content = _strip_hashlines(content)

    # A write with no body is a malformed call, not a request for an empty file -
    # the body went missing between the model and here. Saying so lets the model
    # resend it; writing it out would quietly destroy an existing file.
    if not content.strip():
        existing = os.path.isfile(filepath) and os.path.getsize(filepath) > 0
        return ("[Error] The call carried no content, so nothing was written"
                f"{' (the existing ' + filepath + ' was left untouched)' if existing else ''}. "
                "Send write_file again with the file body in a <content> block right "
                "after the JSON object.")

    preview = content if len(content) <= 200 else content[:200] + "...[truncated]"
    approved = _approval_prompt("Write File", [("path", filepath), ("preview", preview)],
                                rule=f"write_file({filepath})")
    if not approved: return "[System] User denied file write."
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"[Success] File written: {filepath}"
    except Exception as e:
        return f"[Error] Cannot write file: {e}"

def handle_edit_file(filepath: str, old_content: str, new_content: str) -> str:
    """Replace part of a file. Three ways of saying which part, in this order.

    1. `new_content` rows that each carry their own anchor - `38:ff|print()` -
       replace the line each one names, and `old_content` is not needed at all.
       The shortest form there is, and the one that never repeats the old text.
    2. `old_content` made of anchors - `50:1f`, the whole row
       `50:1f|print(answer)`, several rows, or a span `50:1f-53:9c` - names the
       lines, and `new_content` is what replaces them. This is the form to use
       when the number of lines changes, or when they are to be deleted.
    3. Anything else in `old_content` is matched as literal text, exactly as it
       always was.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            file_content = f.read()
    except Exception as e:
        return f"[Error] Cannot read file: {e}"

    # Before `_strip_hashlines`, which would eat the very anchors this reads.
    patch = _parse_patch(new_content)
    if patch is not None:
        return _edit_by_patch(filepath, file_content, patch, old_content)

    new_content = _strip_hashlines(new_content)

    if not (old_content or "").strip():
        return ("[Error] old_content was empty, so nothing was named to replace. "
                "Either put the hashline anchors of the lines in old_content "
                "(e.g. 50:1f), or put the anchor in front of each new line "
                "(50:1f|<the new line>) and leave old_content out.")

    start, end, problem = _anchor_target(filepath, file_content, old_content)
    if problem:
        return problem
    if start is not None:
        return _edit_by_anchor(filepath, file_content, start, end, new_content)

    old_content = _strip_hashlines(old_content)
    if old_content not in file_content:
        return ("[Error] The specified old_content was not found in the file. If the "
                "exact text is hard to reproduce, read_file the file and put the "
                "line's hashline anchor (e.g. 50:1f) in old_content instead.")
    count = file_content.count(old_content)
    if count > 1:
        return (f"[Error] old_content found {count} times. Give a longer snippet, or "
                f"put the line's hashline anchor (e.g. 50:1f, from read_file) in "
                f"old_content instead - a line number is never ambiguous.")

    old_preview = _preview(old_content)
    new_preview = _preview(new_content)
    approved = _approval_prompt("Edit File", [("path", filepath), ("from", old_preview), ("to", new_preview)],
                                rule=f"edit_file({filepath})")
    if not approved: return "[System] User denied file edit."

    new_file_content = file_content.replace(old_content, new_content, 1)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(new_file_content)
        return f"[Success] File edited: {filepath}"
    except Exception as e:
        return f"[Error] Cannot write file: {e}"

def handle_delete_file(filepath: str) -> str:
    approved = _approval_prompt("Delete File", [("path", filepath)], rule=f"delete_file({filepath})")
    if not approved: return "[System] User denied file deletion."
    try:
        os.remove(filepath)
        return f"[Success] File deleted: {filepath}"
    except Exception as e:
        return f"[Error] Cannot delete file: {e}"

def handle_copy_file(src: str, dst: str) -> str:
    approved = _approval_prompt("Copy File", [("from", src), ("to", dst)], rule=f"copy_file({src})")
    if not approved: return "[System] User denied file copy."
    try:
        shutil.copy2(src, dst)
        return f"[Success] File copied to: {dst}"
    except Exception as e:
        return f"[Error] Cannot copy file: {e}"

def handle_create_dir(dirpath: str) -> str:
    approved = _approval_prompt("Create Directory", [("path", dirpath)], rule=f"create_dir({dirpath})")
    if not approved: return "[System] User denied directory creation."
    try:
        os.makedirs(dirpath, exist_ok=True)
        return f"[Success] Directory created: {dirpath}"
    except Exception as e:
        return f"[Error] Cannot create directory: {e}"

def handle_get_url(url: str) -> str:
    try:
        response = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "text/html" in content_type:
            content = strip_html(response.text)
        else:
            content = response.text
        if not config.RETURN_ALL_FILE_CONTENT and len(content) > config.FILE_MAX_DISPLAY_LENGTH:
            content = content[:config.FILE_MAX_DISPLAY_LENGTH] + "\n...[Too long]..."
        return content
    except Exception as e:
        return f"[Error] Cannot fetch URL: {e}"

def _as_option_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalise_questions(what_do, prompt, questions) -> list[dict]:
    """Accept every shape a model might send and return [{question, options}].

    The preferred shape is a list of questions, each with its own options. The
    old single-question form (`what_do` plus a flat `prompt` list of choices) is
    still understood, because that is what half the models will keep emitting.
    """
    items = []

    if isinstance(questions, dict):
        questions = [questions]
    if isinstance(questions, list):
        for entry in questions:
            if isinstance(entry, dict):
                text = str(entry.get("question") or entry.get("prompt")
                           or entry.get("what_do") or "").strip()
                options = _as_option_list(entry.get("options") or entry.get("choices")
                                          or entry.get("answers"))
            else:
                text, options = str(entry).strip(), []
            if text:
                items.append({"question": text, "options": options})
    elif isinstance(questions, str) and questions.strip():
        items.append({"question": questions.strip(), "options": []})

    if items:
        return items

    # The old form: one question in `what_do`, its choices in `prompt`.
    text = str(what_do or "").strip()
    options = _as_option_list(prompt)
    if not text and options:
        text, options = options[0], options[1:]
    if text:
        items.append({"question": text, "options": options})
    return items


def _ask_one(question: str, options: list[str], index: int, total: int) -> str | None:
    """Show one question with its own choices. None means the user gave up."""
    counter = f" {S.MUTED}({index}/{total}){S.R}" if total > 1 else ""
    print(f"\n  {S.INFO}?{S.R} {S.BOLD}Input Required{S.R}{counter}")
    print(f"  {S.MUTED}\u2502{S.R}  {question}\n  {S.MUTED}\u2502{S.R}")
    for i, option in enumerate(options, 1):
        print(f"  {S.MUTED}\u2502{S.R}  {S.ACCENT}{i}.{S.R} {option}")
    custom_idx = len(options) + 1
    print(f"  {S.MUTED}\u2502{S.R}  {S.GRAY}{custom_idx}.  Custom Input{S.R}\n  {S.MUTED}\u2502{S.R}")

    while True:
        try:
            if options:
                raw = input(f"  {S.MUTED}\u2570\u2500{S.R} {S.INFO}Chosen{S.R} "
                            f"{S.MUTED}(1~{custom_idx}){S.R} {S.INFO}\u203a{S.R} ").strip()
                if not raw:
                    continue
                choice = int(raw)
                if 1 <= choice <= len(options):
                    return options[choice - 1]
                if choice == custom_idx:
                    return config.safe_text(input(f"  {S.INFO}  \u203a{S.R} ").strip())
                print(f"  {S.ERR}    Input 1 to {custom_idx} number.{S.R}")
            else:
                answer = config.safe_text(input(f"  {S.MUTED}\u2570\u2500{S.R} {S.INFO}\u203a{S.R} ").strip())
                if answer:
                    return answer
        except ValueError:
            print(f"  {S.ERR}    Input correct number.{S.R}")
        except (EOFError, KeyboardInterrupt):
            print()
            return None


def handle_get_input(what_do="", prompt=None, questions=None) -> str:
    items = _normalise_questions(what_do, prompt, questions)
    if not items:
        return "[Error] No question was given. Pass 'questions': [{'question': ..., 'options': [...]}]."

    answers = []
    for i, item in enumerate(items, 1):
        answer = _ask_one(item["question"], item["options"], i, len(items))
        if answer is None:
            if not answers:
                return "[System] User cancelled the questions without answering."
            return ("[System] User stopped after answering the first "
                    f"{len(answers)} question(s):\n" + _format_answers(answers))
        answers.append((item["question"], answer))

    if len(answers) == 1:
        return answers[0][1]
    return _format_answers(answers)


def _format_answers(answers: list[tuple[str, str]]) -> str:
    lines = []
    for i, (question, answer) in enumerate(answers, 1):
        lines.append(f"{i}. {question}\n   -> {answer}")
    return "\n".join(lines)

def handle_list_dir(dirpath: str) -> str:
    if not os.path.exists(dirpath):
        return f"[Error] Directory not found: {dirpath}"
    try:
        items = os.listdir(dirpath)
        formatted_items = []
        for item in items:
            if os.path.isdir(os.path.join(dirpath, item)):
                formatted_items.append(f"{item}/ (Dir)")
            else:
                formatted_items.append(f"{item} (File)")
        return "\n".join(formatted_items) if formatted_items else "[Empty Directory]"
    except Exception as e:
        return f"[Error] list_dir failed: {e}"

def handle_git_status() -> str:
    try:
        res = subprocess.run(["git", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        return res.stdout or res.stderr
    except Exception as e:
        return f"[Error] git status failed: {e}"

def handle_git_diff() -> str:
    try:
        res = subprocess.run(["git", "diff"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        return res.stdout or res.stderr
    except Exception as e:
        return f"[Error] git diff failed: {e}"

def handle_get_system_info() -> str:
    try:
        cpu_percent = psutil.cpu_percent(interval=0.5)
        cpu_count = psutil.cpu_count(logical=True)

        mem = psutil.virtual_memory()
        mem_total_gb = mem.total / (1024 ** 3)
        mem_used_gb = mem.used / (1024 ** 3)

        disk = psutil.disk_usage(os.getcwd())
        disk_total_gb = disk.total / (1024 ** 3)
        disk_used_gb = disk.used / (1024 ** 3)

        processes = []
        for p in psutil.process_iter(['pid', 'name', 'memory_percent', 'cpu_percent']):
            try:
                processes.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        top_processes = sorted(processes, key=lambda x: x['memory_percent'] or 0, reverse=True)[:5]
        proc_lines = [
            f"  - PID {p['pid']}: {p['name']} (RAM: {p['memory_percent']:.1f}%)"
            for p in top_processes
        ]

        info = (
            f"[System Status]\n"
            f"\u2022 CPU Usage: {cpu_percent}% ({cpu_count} cores)\n"
            f"\u2022 RAM Usage: {mem.percent}% ({mem_used_gb:.1f}GB / {mem_total_gb:.1f}GB)\n"
            f"\u2022 Disk Usage: {disk.percent}% ({disk_used_gb:.1f}GB / {disk_total_gb:.1f}GB)\n\n"
            f"[Top Memory Processes]\n" + "\n".join(proc_lines)
        )
        return info

    except Exception as e:
        return f"[Error] Failed to fetch system info: {e}"


def handle_search_in_file(query: str, is_regex: bool = False) -> str:
    if not query:
        return "[Error] Search query is required."
    try:
        if is_regex:
            try:
                pattern = re.compile(query)
            except re.error as e:
                return f"[Error] Invalid regex pattern: {e}"
        else:
            pattern = re.compile(re.escape(query))

        matches = []
        max_results = 100
        search_root = os.getcwd()

        for root, dirs, files in os.walk(search_root):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('venv', '__pycache__', 'node_modules', '.git')]
            for filename in files:
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, search_root)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        for line_num, line in enumerate(f, 1):
                            if pattern.search(line):
                                line_preview = line.rstrip()
                                if len(line_preview) > 200:
                                    line_preview = line_preview[:200] + "..."
                                matches.append(f"{rel_path}:{line_num}: {line_preview}")
                                if len(matches) >= max_results:
                                    break
                except (PermissionError, OSError):
                    continue
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        if not matches:
            return f"[Search] No matches found for: {query}"
        header = f"[Search] Found {len(matches)} match(es) for '{query}':\n"
        if len(matches) >= max_results:
            header = f"[Search] Showing first {max_results} matches for '{query}' (more may exist):\n"
        return header + "\n".join(matches)
    except Exception as e:
        return f"[Error] Search failed: {e}"


def handle_call_api(url: str, method: str, headers: str = "", payload: str = "") -> str:
    if not url:
        return "[Error] URL is required."
    if not method:
        return "[Error] HTTP method is required."

    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return f"[Error] Unsupported HTTP method: {method}"

    approved = _approval_prompt("API Call", [("URL", url), ("Method", method)], rule=f"call_api({url})")
    if not approved:
        return "[System] User denied API call."

    try:
        req_headers = {"User-Agent": "LocalChat/1.0"}
        if headers:
            if isinstance(headers, str):
                try:
                    parsed = json.loads(headers)
                    if isinstance(parsed, dict):
                        req_headers.update(parsed)
                except json.JSONDecodeError:
                    return "[Error] Invalid headers JSON format. Expected a JSON object like {\"Key\": \"Value\"}."
            elif isinstance(headers, dict):
                req_headers.update(headers)

        req_body = None
        if payload:
            if isinstance(payload, str):
                try:
                    req_body = json.loads(payload)
                except json.JSONDecodeError:
                    req_body = payload
            elif isinstance(payload, dict):
                req_body = payload

        if method == "GET":
            resp = requests.get(url, headers=req_headers, timeout=30)
        elif method == "POST":
            resp = requests.post(url, headers=req_headers, json=req_body if isinstance(req_body, dict) else None, data=req_body if isinstance(req_body, str) else None, timeout=30)
        elif method == "PUT":
            resp = requests.put(url, headers=req_headers, json=req_body if isinstance(req_body, dict) else None, data=req_body if isinstance(req_body, str) else None, timeout=30)
        elif method == "PATCH":
            resp = requests.patch(url, headers=req_headers, json=req_body if isinstance(req_body, dict) else None, data=req_body if isinstance(req_body, str) else None, timeout=30)
        elif method == "DELETE":
            resp = requests.delete(url, headers=req_headers, timeout=30)

        content = resp.text
        if not config.RETURN_ALL_FILE_CONTENT and len(content) > config.FILE_MAX_DISPLAY_LENGTH:
            content = content[:config.FILE_MAX_DISPLAY_LENGTH] + "\n...[Too long]..."

        result = (
            f"[API Response]\n"
            f"\u2022 Status: {resp.status_code} {resp.reason}\n"
            f"\u2022 Content-Type: {resp.headers.get('Content-Type', 'unknown')}\n\n"
            f"{content}"
        )
        return result
    except requests.exceptions.Timeout:
        return "[Error] API request timed out (30s)."
    except requests.exceptions.ConnectionError:
        return f"[Error] Could not connect to: {url}"
    except Exception as e:
        return f"[Error] API call failed: {e}"



def _get_ts_parser(lang_name: str):
    lang = _TS_LANGUAGES.get(lang_name)
    if lang is None:
        return None, None
    parser = Parser(lang)
    return parser, lang


def _detect_language(filepath: str) -> str | None:
    _, ext = os.path.splitext(filepath)
    return _EXT_TO_LANG.get(ext.lower())


def _extract_params_python(node, src: bytes) -> list[dict]:
    params = []
    param_node = None
    for child in node.children:
        if child.type == "parameters":
            param_node = child
            break
    if param_node is None:
        return params
    for child in param_node.children:
        if child.type in ("identifier",):
            params.append({"name": child.text.decode(), "type": None, "default": None})
        elif child.type == "typed_parameter":
            name = default = ptype = None
            for sub in child.children:
                if sub.type == "identifier" and name is None:
                    name = sub.text.decode()
                elif sub.type == "type":
                    ptype = sub.text.decode()
            params.append({"name": name, "type": ptype, "default": None})
        elif child.type == "default_parameter":
            name = default = None
            for sub in child.children:
                if sub.type == "identifier" and name is None:
                    name = sub.text.decode()
                elif sub.type not in ("=",):
                    default = sub.text.decode()
            params.append({"name": name, "type": None, "default": default})
        elif child.type == "typed_default_parameter":
            name = default = ptype = None
            for sub in child.children:
                if sub.type == "identifier" and name is None:
                    name = sub.text.decode()
                elif sub.type == "type":
                    ptype = sub.text.decode()
                elif sub.type not in ("=", ":"):
                    default = sub.text.decode()
            params.append({"name": name, "type": ptype, "default": default})
        elif child.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            prefix = "*" if child.type == "list_splat_pattern" else "**"
            for sub in child.children:
                if sub.type == "identifier":
                    params.append({"name": prefix + sub.text.decode(), "type": None, "default": None})
    return params


def _extract_params_generic(node, src: bytes) -> list[dict]:
    params = []
    for child in node.children:
        if child.type in ("formal_parameters", "parameter_list", "parameters",
                          "formal_parameter_list"):
            for p in child.named_children:
                params.append({"name": p.text.decode(), "type": None, "default": None})
            break
    return params


def _extract_return_type(node, src: bytes) -> str | None:
    for child in node.children:
        if child.type == "type":
            return child.text.decode()
        if child.type == "return_type":
            return child.text.decode()
    return None


def _extract_decorators(node, src: bytes) -> list[str]:
    decorators = []
    sibling = node.prev_named_sibling
    while sibling and sibling.type == "decorator":
        decorators.insert(0, sibling.text.decode())
        sibling = sibling.prev_named_sibling
    for child in node.children:
        if child.type == "decorator":
            decorators.append(child.text.decode())
    return decorators


_FUNC_TYPES = {
    "function_definition", "function_declaration",
    "method_definition", "method_declaration",
    "arrow_function", "generator_function_declaration",
    "function_item",
}
_CLASS_TYPES = {
    "class_definition", "class_declaration",
    "struct_item", "enum_item", "impl_item",
    "interface_declaration", "enum_declaration",
    "type_declaration",
}


def _walk_skeleton(node, src: bytes, lang: str) -> list[dict]:
    results = []
    for child in node.children:
        entry = None
        if child.type in _FUNC_TYPES:
            name_node = child.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "<anonymous>"
            if lang == "python":
                params = _extract_params_python(child, src)
            else:
                params = _extract_params_generic(child, src)
            ret = _extract_return_type(child, src)
            decorators = _extract_decorators(child, src) if lang == "python" else []
            entry = {
                "type": "function",
                "name": name,
                "line": child.start_point[0] + 1,
                "end_line": child.end_point[0] + 1,
                "parameters": params,
                "return_type": ret,
            }
            if decorators:
                entry["decorators"] = decorators
            body = child.child_by_field_name("body")
            if body:
                inner = _walk_skeleton(body, src, lang)
                if inner:
                    entry["children"] = inner

        elif child.type in _CLASS_TYPES:
            name_node = child.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "<anonymous>"
            bases = []
            for sub in child.children:
                if sub.type in ("argument_list", "superclass", "type_list",
                                "class_heritage"):
                    bases.append(sub.text.decode())
            entry = {
                "type": "class",
                "name": name,
                "line": child.start_point[0] + 1,
                "end_line": child.end_point[0] + 1,
                "bases": bases,
            }
            body = child.child_by_field_name("body")
            if body:
                inner = _walk_skeleton(body, src, lang)
                if inner:
                    entry["methods"] = inner
            else:
                inner = _walk_skeleton(child, src, lang)
                if inner:
                    entry["methods"] = inner

        if entry:
            results.append(entry)
        else:
            deeper = _walk_skeleton(child, src, lang)
            results.extend(deeper)
    return results


def handle_get_code_skeleton(file_path: str) -> str:
    if not TREE_SITTER_AVAILABLE:
        return "[Error] tree-sitter is not installed. Run: pip install tree-sitter tree-sitter-python (and other language grammars)"

    if not file_path:
        return "[Error] file_path is required."

    if not os.path.isfile(file_path):
        return f"[Error] File not found: {file_path}"

    lang_name = _detect_language(file_path)
    if lang_name is None:
        return (f"[Error] Unsupported file extension. "
                f"Supported: {', '.join(sorted(set(_EXT_TO_LANG.values())))}")

    parser, lang = _get_ts_parser(lang_name)
    if parser is None:
        return f"[Error] Language grammar not available: {lang_name}"

    try:
        with open(file_path, "rb") as f:
            src = f.read()
    except Exception as e:
        return f"[Error] Failed to read file: {e}"

    tree = parser.parse(src)
    skeleton = _walk_skeleton(tree.root_node, src, lang_name)

    result = {
        "file": file_path,
        "language": lang_name,
        "skeleton": skeleton,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


def handle_query_ast_node(file_path: str, pattern: str, language: str = "") -> str:
    if not TREE_SITTER_AVAILABLE:
        return "[Error] tree-sitter is not installed."

    if not file_path:
        return "[Error] file_path is required."
    if not pattern:
        return "[Error] pattern is required."

    if not os.path.isfile(file_path):
        return f"[Error] File not found: {file_path}"

    lang_name = language.strip().lower() if language else _detect_language(file_path)
    if not lang_name or lang_name not in _TS_LANGUAGES:
        return (f"[Error] Could not determine language. "
                f"Supported: {', '.join(sorted(_TS_LANGUAGES.keys()))}")

    parser, lang = _get_ts_parser(lang_name)
    if parser is None:
        return f"[Error] Language grammar not available: {lang_name}"

    try:
        with open(file_path, "rb") as f:
            src = f.read()
    except Exception as e:
        return f"[Error] Failed to read file: {e}"

    tree = parser.parse(src)

    try:
        query = Query(lang, pattern)
    except Exception as e:
        return f"[Error] Invalid Tree-sitter query pattern: {e}"

    cursor = QueryCursor(query)
    matches_raw = list(cursor.matches(tree.root_node))

    if not matches_raw:
        return json.dumps({"file": file_path, "pattern": pattern, "matches": []},
                          indent=2, ensure_ascii=False)

    lines = src.split(b"\n")
    results = []
    max_results = 50

    for pattern_idx, captures in matches_raw:
        if len(results) >= max_results:
            break
        for cap_name, nodes in captures.items():
            for node in nodes:
                if len(results) >= max_results:
                    break
                start_line = node.start_point[0]
                end_line = node.end_point[0]
                ctx_start = max(0, start_line - 1)
                ctx_end = min(len(lines) - 1, end_line + 1)
                if ctx_end - ctx_start > 10:
                    ctx_end = ctx_start + 10
                snippet_lines = []
                for i in range(ctx_start, ctx_end + 1):
                    prefix = ">>>" if start_line <= i <= end_line else "   "
                    snippet_lines.append(f"{prefix} {i + 1}: {lines[i].decode(errors='replace')}")
                results.append({
                    "capture": cap_name,
                    "node_type": node.type,
                    "text": node.text.decode(errors="replace"),
                    "start_line": start_line + 1,
                    "end_line": end_line + 1,
                    "start_col": node.start_point[1],
                    "end_col": node.end_point[1],
                    "snippet": "\n".join(snippet_lines),
                })

    output = {
        "file": file_path,
        "language": lang_name,
        "pattern": pattern,
        "total_matches": len(results),
        "matches": results,
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


def handle_submit_plan_for_approval(context_discovered: str, diff_blueprint: str, verification_steps: str) -> str:
    print(f"\n  {S.INFO}?{S.R} {S.BOLD}Plan Approval Required{S.R}")
    print(f"  {S.MUTED}│{S.R}  {S.ACCENT}[Context Discovered]{S.R}")
    for line in context_discovered.splitlines(): print(f"  {S.MUTED}│{S.R}    {line}")
    print(f"  {S.MUTED}│{S.R}  {S.ACCENT}[Diff Blueprint]{S.R}")
    for line in diff_blueprint.splitlines(): print(f"  {S.MUTED}│{S.R}    {line}")
    print(f"  {S.MUTED}│{S.R}  {S.ACCENT}[Verification Steps]{S.R}")
    for line in verification_steps.splitlines(): print(f"  {S.MUTED}│{S.R}    {line}")
    
    if config.AUTO_ALLOW:
        print(f"  {S.MUTED}│{S.R}  {S.OK}Auto-approved due to AUTOMODE.{S.R}")
        return "[System] AUTOMODE is ON. Plan automatically approved. Proceed strictly with execution and verification."

    print(f"  {S.MUTED}│{S.R}")
    print(f"  {S.MUTED}│{S.R}  {S.BOLD}1.{S.R} Approve (Proceed with execution)")
    print(f"  {S.MUTED}│{S.R}  {S.BOLD}2.{S.R} Reject (Abort task)")
    print(f"  {S.MUTED}│{S.R}  {S.BOLD}3.{S.R} Revise (Provide custom feedback)")
    print(f"  {S.MUTED}│{S.R}")
    
    while True:
        try:
            choice_str = input(f"  {S.MUTED}╰─{S.R} {S.INFO}Select{S.R} {S.MUTED}(1~3){S.R} {S.INFO}›{S.R} ").strip()
            if not choice_str: continue
            choice = int(choice_str)
            if choice == 1:
                return "[System] Plan Approved by User. You may now execute the plan strictly within the approved blueprint. Conclude by executing the verification steps."
            elif choice == 2:
                return "[System] Plan Rejected by User. Abort the task."
            elif choice == 3:
                feedback = input(f"  {S.INFO}  › Please enter feedback: {S.R}").strip()
                return f"[System] Plan Rejected with feedback: {feedback}\nPlease revise your plan and submit again."
            else:
                print(f"  {S.ERR}    Input 1 to 3.{S.R}")
        except ValueError:
            print(f"  {S.ERR}    Input correct number.{S.R}")


def handle_mcp_tool_call(function_name: str, arguments: dict) -> str:
    """Run a tool that lives on an attached MCP server."""
    resolved = mcp_client.resolve_tool(function_name)
    if resolved is None:
        available = ", ".join(mcp_client.available_tool_names()) or "(none)"
        return f"[Error] No MCP tool named '{function_name}'. Available MCP tools: {available}"

    server, tool = resolved
    tool_name = str(tool.get("name", ""))

    if not mcp_client.auto_approved(server, tool_name):
        details = [("server", server.name), ("tool", tool_name)]
        for key, value in arguments.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            details.append((key, text))
        if not _approval_prompt("MCP Tool Call", details):
            return f"[System] User denied the MCP call '{tool_name}' on server '{server.name}'."

    return mcp_client.call_tool(server, tool_name, arguments)


def handle_list_mcp_resources(server_name: str = "") -> str:
    return mcp_client.list_resources_text(server_name)


def handle_read_mcp_resource(uri: str, server_name: str = "") -> str:
    return mcp_client.read_resource_text(uri, server_name)


def handle_spawn_agent(task: str, context: str = "", model: str = "") -> str:
    """Delegate a self-contained job to a fresh agent. See subagent.py.

    Approval is asked for here rather than left to the sub-agent's own tool
    calls. Those are checked too, but a sub-agent costs a stretch of time and,
    on a hosted model, real money before it reaches its first tool - so the
    decision to hire one is the user's, the same as running a command is.
    """
    from simple_harness import subagent
    first_line = (task or "").strip().splitlines()[0] if (task or "").strip() else ""
    details = [("task", first_line), ("model", model or "same as this one")]
    if not _approval_prompt("Hire Sub-agent", details, rule="spawn_agent"):
        return ("[System] The user declined to start a sub-agent. Do this part of "
                "the work yourself.")
    return subagent.run(task, context, model)


# ---------------------------------------------------------------------------
# talking to the other agents working here - see channel.py
# ---------------------------------------------------------------------------

def _channel_off() -> str:
    return ("[System] The agent channel is off, so this session cannot see or "
            "reach other agents. Tell the user; /agents on turns it back on.")


def handle_list_agents() -> str:
    """Who else is working in this project, and on what."""
    if not channel.enabled():
        return _channel_off()
    here = channel.agents()
    mine = channel.me()
    if len(here) <= 1:
        return (f"[Agents] You ({mine or 'this session'}) are the only agent working "
                f"in {channel.workspace()} right now. Nothing to coordinate.")
    lines = []
    for record in here:
        holds = ", ".join(record.get("holds") or []) or "nothing"
        who = f"{record['id']} (you)" if record["id"] == mine else record["id"]
        lines.append(f"  {who} - {record.get('label') or 'unknown model'} - "
                     f"started {channel.ago(record.get('started'))} - "
                     f"holding: {holds}")
    return (f"[Agents] {len(here)} agents are working in {channel.workspace()}:\n"
            + "\n".join(lines)
            + "\nA file another agent is holding cannot be edited from here until "
              "they release it. Ask them with send_agent_message.")


def handle_send_agent_message(message: str, to: str = "") -> str:
    """Say something to another agent. Nothing is queued for one that is not here."""
    if not channel.enabled():
        return _channel_off()
    ok, said = channel.send(message, to)
    if not ok:
        # `[System]`, not `[Error]`: there is nobody to deliver to, so this is a
        # closed door rather than a call that went wrong, and the turn should end
        # if the model keeps knocking on it (ARCHITECTURE 5.9).
        return (f"[System] The message was not sent: {said}. Use list_agents to see "
                f"who is actually here, and do not retry the same message.")
    print(f"  {S.MUTED}\u2192 {channel.me()} to {to or 'everyone'}: "
          f"{' '.join(str(message).split())[:70]}{S.R}")
    return (f"[Success] Message {said}. They will read it on their next turn - "
            f"an answer, if one comes, will reach you the same way. Carry on with "
            f"something else in the meantime.")


def handle_claim_files(paths: str, reason: str = "") -> str:
    """Take the files, or report who already has them. Never takes some of them."""
    if not channel.enabled():
        return _channel_off()
    if not channel.me():
        return ("[System] This session is not on the agent board, so it cannot claim "
                "files. Nothing else is claiming them either - carry on.")
    wanted = channel.split_paths(paths)
    if not wanted:
        return "[Error] No file path was given to claim."
    taken, refused = channel.claim(paths, reason)
    if refused:
        held = "; ".join(f"{c['path']} is held by {c['agent']} ({c.get('reason') or 'no reason given'})"
                         for c in refused)
        return (f"[System] Nothing was claimed - {held}. Nothing is claimed at all "
                f"when any one file is taken, so you are not half-holding the rest. "
                f"Ask them with send_agent_message, or work on something else.")
    print(f"  {S.MUTED}\u25c6 claimed {', '.join(taken)}{S.R}")
    return (f"[Success] Claimed {', '.join(taken)}. No other agent can edit them "
            f"until you call release_files, so release them as soon as you are done.")


def handle_release_files(paths: str) -> str:
    """Give files back. Only this agent's own claims are dropped."""
    if not channel.enabled():
        return _channel_off()
    dropped = channel.release(paths)
    if not dropped:
        return ("[System] Nothing was released - none of those files are claimed by "
                "you. Use list_agents to see what you are holding.")
    return f"[Success] Released {', '.join(dropped)}."


# Which argument of which tool names a file that ends up changed. `create_dir`
# is absent on purpose: git does not track an empty directory, so there would
# be nothing to commit.
_WRITES_FILES = {
    "write_file": ("filepath",),
    "edit_file": ("filepath",),
    "delete_file": ("filepath",),
    "copy_file": ("dst",),
}


# Anything that changes the world rather than reporting on it. `run_cmd` and
# `send_input` are here because a command can write as easily as it can read,
# and a stage that is meant to be read-only cannot check which.
_CHANGES_THINGS = frozenset(_WRITES_FILES) | {
    "create_dir", "run_cmd", "send_input", "end_process", "run_python",
    "write_memory", "edit_memory", "delete_memory", "call_api",
}


def _claimed_by_another(function_name: str, arguments: dict) -> tuple:
    """The other agent's claim standing in front of this call, or ("", {}).

    Read from `_WRITES_FILES`, so a tool that changes a file is covered here the
    moment it is added there - there is no second list of file tools to keep in
    step. `run_cmd` is not covered and cannot be: what a shell command touches
    is not knowable from the call.
    """
    for key in _WRITES_FILES.get(function_name, ()):
        path = arguments.get(key)
        if isinstance(path, str) and path:
            conflict = channel.holder(path)
            if conflict:
                return path, conflict
    return "", {}


def _paths_written(function_name: str, arguments: dict, result) -> list:
    """The files a call actually changed. Empty unless the tool reported success.

    A write the user declined at the approval prompt, or one that failed, has
    changed nothing - nothing to commit, and nothing to tell the other agents
    about. Both of the things that happen after a write read this, so they
    cannot disagree about what was written.
    """
    keys = _WRITES_FILES.get(function_name)
    if not keys or not isinstance(result, str) or not result.startswith("[Success"):
        return []
    return [arguments[key] for key in keys
            if isinstance(arguments.get(key), str) and arguments[key]]


def _commit_if_changed(function_name: str, arguments: dict, result) -> None:
    """Commit what a tool just wrote, so it can be taken back later."""
    if not git_ops.enabled():
        return
    paths = _paths_written(function_name, arguments, result)
    if not paths:
        return
    sha = git_ops.auto_commit(paths, function_name)
    if sha:
        print(f"  {S.MUTED}⎇ committed {sha}{S.R}  {S.GRAY}/undo to take it back{S.R}")


# An error used to arrive as the error and nothing else: "[Error] Command failed
# (exit code 1)." followed by a traceback. Which command? Which file? The model
# had to work that out from its own memory of what it asked for, and a small one
# often gets it wrong - it fixes the file it was thinking about rather than the
# one that broke. Naming the call costs one line and removes the guess.
_ERROR_PREFIX = config.TOOL_ERROR_PREFIX
_ARG_VALUE_CHARS = 60          # a file body must not push the error off the top
_ARG_LINE_CHARS = 200


def _describe_call(function_name: str, arguments: dict) -> str:
    """`run_cmd(command='python3 boom.py')` - the call, short enough to read."""
    parts = []
    for key, value in (arguments or {}).items():
        text = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, default=str)
        text = " ".join(str(text).split())       # a whole file on one line
        if len(text) > _ARG_VALUE_CHARS:
            text = text[:_ARG_VALUE_CHARS] + "..."
        parts.append(f"{key}={text!r}")
    joined = ", ".join(parts)
    if len(joined) > _ARG_LINE_CHARS:
        joined = joined[:_ARG_LINE_CHARS] + "..."
    return f"{function_name}({joined})"


def _name_the_failure(function_name: str, arguments: dict, result):
    """Put the call that failed in front of what it said about failing.

    Only `[Error]`. A `[System]` result is a refusal that already says which
    tool it refused, and `llm_client` counts those to stop a model knocking on
    a closed door - rewriting that prefix would break the counter.
    """
    if not isinstance(result, str) or not result.startswith(_ERROR_PREFIX):
        return result
    said = result[len(_ERROR_PREFIX):].lstrip()
    return f"{_ERROR_PREFIX} {_describe_call(function_name, arguments)}: {said}"


def dispatch_tool(function_name: str, arguments: dict) -> str | None:
    # Small models sometimes emit "arguments" as a JSON string rather than an
    # object. Normalise once here so no handler has to defend against it.
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    _fmt_tool_call(function_name, arguments)

    if config.DEEPTHINK_READONLY and function_name in _CHANGES_THINGS:
        # Telling a model not to edit yet is not enough - it edits anyway, and
        # then the stages that were meant to review a plan are reviewing work
        # nobody planned. So the planning stages are read-only for real.
        print(f"  {S.MUTED}✗ not yet: this stage is for working it out, "
              f"not changing it{S.R}")
        return (f"[System] '{function_name}' is not available during this stage. "
                "You are working out what to do, not doing it - say what you would "
                "change and why. You will be asked to make the change in a later "
                "stage, and everything you write now carries over to it.")

    blocked, conflict = _claimed_by_another(function_name, arguments)
    if conflict:
        # Another harness, in another terminal, is in the middle of this file.
        # Letting the write through would throw their work away silently, which
        # is the whole reason the board exists.
        print(f"  {S.WARN}\u2717 {conflict['agent']} is working on "
              f"{conflict['path']}{S.R}  {S.GRAY}/agents to see who{S.R}")
        return channel.refusal(function_name, blocked, conflict)

    verdict, rule = permissions.decide(function_name, arguments)
    if verdict == "deny":
        print(f"  {S.ERR}✗ Blocked by permission rule: {rule}{S.R}")
        return (f"[System] '{function_name}' is blocked by your permission rules "
                f"(rule: {rule}). Do not retry it; tell the user it is blocked.")

    config.POLICY_AUTO_ALLOW = verdict == "allow"
    try:
        result = _run_tool(function_name, arguments)
    finally:
        config.POLICY_AUTO_ALLOW = False
    _commit_if_changed(function_name, arguments, result)
    channel.note_write(_paths_written(function_name, arguments, result))
    return _name_the_failure(function_name, arguments, result)


def _handlers() -> dict:
    """Tool name to the function that runs it.

    `session` is imported here rather than at the top only because it keeps the
    memory handlers next to the rest of the table; there is no cycle either way.
    """
    global _HANDLERS
    if _HANDLERS is None:
        from simple_harness.session import (handle_write_memory, handle_get_memory_list,
                             handle_read_memory, handle_delete_memory,
                             handle_edit_memory)
        _HANDLERS = {
            "search_web": handle_search_web,
            "get_url": handle_get_url,
            "run_cmd": safe_run_cmd,
            "send_input": handle_send_input,
            "end_process": handle_end_process,
            "run_python": handle_run_python,
            "list_dir": handle_list_dir,
            "read_file": handle_read_file,
            "write_file": handle_write_file,
            "edit_file": handle_edit_file,
            "delete_file": handle_delete_file,
            "copy_file": handle_copy_file,
            "create_dir": handle_create_dir,
            "git_status": handle_git_status,
            "git_diff": handle_git_diff,
            "write_memory": handle_write_memory,
            "get_memory_list": handle_get_memory_list,
            "read_memory": handle_read_memory,
            "delete_memory": handle_delete_memory,
            "edit_memory": handle_edit_memory,
            "get_user_input": handle_get_input,
            "get_system_info": handle_get_system_info,
            "search_in_file": handle_search_in_file,
            "call_api": handle_call_api,
            "get_code_skeleton": handle_get_code_skeleton,
            "query_ast_node": handle_query_ast_node,
            "submit_plan_for_approval": handle_submit_plan_for_approval,
            "use_skill": handle_use_skill,
            "spawn_agent": handle_spawn_agent,
            "list_agents": handle_list_agents,
            "send_agent_message": handle_send_agent_message,
            "claim_files": handle_claim_files,
            "release_files": handle_release_files,
        }
        _check_registry(_HANDLERS)
    return _HANDLERS


_HANDLERS = None


def _check_registry(handlers: dict) -> None:
    """Refuse to run with a table and a prompt that disagree.

    This is the whole point of the registry: a tool the model is told about but
    that nothing can run, or a handler the model is never told exists, is a
    silent failure at the worst possible moment. Here it is a startup error.
    """
    described = set(toolspec.names())
    implemented = set(handlers)
    unrunnable = described - implemented
    unadvertised = implemented - described
    problems = []
    if unrunnable:
        problems.append(f"described to the model but not implemented: "
                        f"{', '.join(sorted(unrunnable))}")
    if unadvertised:
        problems.append(f"implemented but not described to the model: "
                        f"{', '.join(sorted(unadvertised))}")
    for tool in toolspec.TOOLS:
        handler = handlers.get(tool.name)
        if handler is None:
            continue
        try:
            wanted = len(inspect.signature(handler).parameters)
        except (TypeError, ValueError):
            continue
        if wanted != len(tool.params):
            problems.append(f"{tool.name}: the spec lists {len(tool.params)} "
                            f"parameter(s), {handler.__name__} takes {wanted}")
    if problems:
        raise RuntimeError("tool registry is inconsistent - "
                           + "; ".join(problems))


def _run_tool(function_name: str, arguments: dict) -> str | None:
    tool = toolspec.get(function_name)
    if tool is not None:
        handler = _handlers().get(tool.name)
        if handler is not None:
            return handler(*tool.bind(arguments))

    if mcp_client.is_mcp_tool(function_name):
        return handle_mcp_tool_call(function_name, arguments)
    if function_name == "list_mcp_resources":
        return handle_list_mcp_resources(arguments.get("server", "")
                                         or arguments.get("server_name", ""))
    if function_name == "read_mcp_resource":
        return handle_read_mcp_resource(arguments.get("uri", ""),
                                        arguments.get("server", "")
                                        or arguments.get("server_name", ""))

    print(f"  {S.WARN}\u26a0 Unknown tool: {function_name}{S.R}")
    return None

"""The conversation loop: stream a reply, read the tool calls out of it, run them.

Nothing here knows which provider is answering - `providers.py` hands over the
same events whether they came from a local Ollama model or a hosted one.
"""

import sys
import re
import json
import asyncio
import itertools
import time
import random
import config
from config import S
from tui import _fmt_tool_call, _fmt_tool_result, _fmt_tokens
from renderer import _render_line, _format_table, _render_full
from tools import dispatch_tool
import providers


TOOL_CALL_TAG = "<tool_call>"
THINK_OPEN = ("<think>", "<thinking>")
THINK_CLOSE = ("</think>", "</thinking>")

_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_START = re.compile(r"<think(?:ing)?>", re.IGNORECASE)


def _holdback(buffer: str, markers: tuple[str, ...]) -> int:
    """Length of the tail that could still grow into one of `markers`."""
    longest = max(len(m) for m in markers) - 1
    for size in range(min(len(buffer), longest), 0, -1):
        tail = buffer[-size:]
        if any(m.startswith(tail) for m in markers):
            return size
    return 0


class StreamFilter:
    """Splits a model's raw stream into prose, reasoning, and the tool call.

    Reasoning models (qwen3, deepseek-r1, gpt-oss) wrap their scratch work in
    `<think>` tags, and every model here announces a tool call with
    `<tool_call>`. Neither is prose and neither belongs on screen as prose. The
    tags arrive a few characters at a time, so a tail that could still grow into
    one is held back rather than printed and regretted.
    """

    def __init__(self):
        self._buffer = ""
        self._state = "prose"        # prose | think | tool
        self.saw_tool_call = False

    @property
    def thinking(self) -> bool:
        return self._state == "think"

    def _markers(self) -> tuple[str, ...]:
        if self._state == "think":
            # A tool call also closes an unterminated think block, so a model
            # that forgets </think> does not swallow its own tool call.
            return THINK_CLOSE + (TOOL_CALL_TAG,)
        return (TOOL_CALL_TAG,) + THINK_OPEN

    def feed(self, text: str) -> list[tuple[str, str]]:
        self._buffer += text
        return self._drain(final=False)

    def close(self) -> list[tuple[str, str]]:
        return self._drain(final=True)

    def _drain(self, final: bool) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        while self._buffer and self._state != "tool":
            markers = self._markers()
            hits = [(self._buffer.find(m), m) for m in markers if m in self._buffer]
            if hits:
                index, marker = min(hits, key=lambda hit: hit[0])
                if index:
                    out.append((self._state, self._buffer[:index]))
                self._buffer = self._buffer[index + len(marker):]
                if marker == TOOL_CALL_TAG:
                    self._state = "tool"
                    self.saw_tool_call = True
                    self._buffer = ""
                else:
                    self._state = "think" if marker in THINK_OPEN else "prose"
                continue

            hold = 0 if final else _holdback(self._buffer, markers)
            keep = len(self._buffer) - hold
            if keep > 0:
                out.append((self._state, self._buffer[:keep]))
                self._buffer = self._buffer[keep:]
            break
        return [(kind, text) for kind, text in out if text]


def strip_thinking(text: str) -> str:
    """Remove reasoning from a response before it is stored or exported."""
    cleaned = _THINK_BLOCK.sub("", text)
    start = _THINK_START.search(cleaned)
    if start:
        # An unterminated block runs to the end - except for a tool call, which
        # still has to survive.
        tail = cleaned[start.end():]
        tool = tail.find(TOOL_CALL_TAG)
        cleaned = cleaned[:start.start()] + (tail[tool:] if tool != -1 else "")
    return cleaned.strip()


async def stream_reply(messages: list[dict]) -> str:

    async def spinner():
        """· ✢ ✳ ✶ ✻ ✽"""
        frames = [
            f"{S.ACCENT}·{S.R}",
            f"{S.ACCENT}✢\uFE0E{S.R}",
            f"{S.ACCENT}*\uFE0E{S.R}",
            f"{S.ACCENT}✶\uFE0E{S.R}",
            f"{S.ACCENT}✻\uFE0E{S.R}",
            f"{S.ACCENT}✽\uFE0E{S.R}",
        ]
        cycle = itertools.cycle(frames)
        try:
            while True:
                frame = next(cycle)
                sys.stdout.write(f'\r  {frame} {S.GRAY}thinking…{S.R}  ')
                sys.stdout.flush()
                await asyncio.sleep(random.randint(2, 4) * 0.1)
        except asyncio.CancelledError:
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()

    spin_task = asyncio.create_task(spinner())
    
    full_text = ""
    thinking_text = ""
    stream = StreamFilter()
    printing = False        # prose has started, so the spinner is gone
    thought_open = False    # a reasoning block is being shown
    think_buffer = ""
    
    line_buffer = ""
    in_code = False
    
    table_buffer = []
    in_latex_block = False

    def flush_table():
        if table_buffer:
            for t_line in _format_table(table_buffer): print(t_line)
            table_buffer.clear()

    def process_line(line: str, in_c: bool) -> bool:
        nonlocal in_latex_block
        stripped = line.strip()
        if not in_c and not in_latex_block and stripped.startswith('|') and stripped.endswith('|'):
            table_buffer.append(line)
            return in_c
        else:
            flush_table()
            rendered, out_c, in_latex_block = _render_line(line, in_c, in_latex_block)
            for r_line in rendered.split('\n'):
                print(r_line)
            return out_c

    def emit(text: str, in_c: bool) -> bool:
        """Buffer streamed prose and render it a completed line at a time."""
        nonlocal line_buffer
        for char in text:
            if char == '\n':
                sys.stdout.write('\r\033[K')
                sys.stdout.flush()
                in_c = process_line(line_buffer, in_c)
                line_buffer = ""
            else:
                line_buffer += char
        return in_c

    def emit_thought(text: str) -> None:
        """Reasoning, shown dimmed and unrendered - it is not the answer."""
        nonlocal think_buffer
        for char in text:
            if char == '\n':
                sys.stdout.write('\r\033[K')
                sys.stdout.flush()
                print(f"  {S.MUTED}│ {think_buffer}{S.R}")
                think_buffer = ""
            else:
                think_buffer += char

    def close_thought() -> None:
        nonlocal think_buffer, thought_open
        if not thought_open:
            return
        if think_buffer.strip():
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()
            print(f"  {S.MUTED}│ {think_buffer}{S.R}")
        think_buffer = ""
        print(f"  {S.MUTED}╰─{S.R}")
        thought_open = False

    prompt_tokens = 0
    completion_tokens = 0
    total_duration = 0.0
    eval_duration = 0.0

    start_time = None
    chunk_count = 0

    try:
        reply = providers.current().stream(messages)

        async for chunk in reply:
            if start_time is None:
                start_time = time.time()

            chunk_count += 1

            content = chunk.get('text', '') or ''
            full_text += content

            if chunk.get('done'):
                prompt_tokens = chunk.get("prompt_tokens", 0)
                completion_tokens = chunk.get("completion_tokens", 0)
                total_duration = chunk.get("total_seconds", 0.0)
                eval_duration = chunk.get("eval_seconds", 0.0)

            # Providers that report reasoning separately hand it over in its own
            # field. It never enters `full_text`, so it stays out of the
            # conversation history for free.
            events = []
            native_thinking = chunk.get('thinking', '') or ''
            if native_thinking:
                events.append(("think", native_thinking))
            events.extend(stream.feed(content))

            for kind, text in events:
                if kind == "think":
                    thinking_text += text
                    if not config.SHOW_THINKING:
                        continue
                    if not printing:
                        printing = True
                        if not spin_task.done():
                            spin_task.cancel()
                            sys.stdout.write('\r\033[K')
                            sys.stdout.flush()
                    if not thought_open:
                        thought_open = True
                        print(f"  {S.MUTED}╭─ thinking{S.R}")
                    emit_thought(text)
                    continue

                close_thought()
                if not printing:
                    printing = True
                    if not spin_task.done():
                        spin_task.cancel()
                        sys.stdout.write('\r\033[K')
                        sys.stdout.flush()
                in_code = emit(text, in_code)

            if stream.saw_tool_call and not line_buffer.strip():
                line_buffer = ""     # drop the blank line the model left before the tag

            if printing and not stream.saw_tool_call:
                elapsed = time.time() - start_time
                if elapsed > 0.1:
                    tps = chunk_count / elapsed
                    sys.stdout.write(f"\r\033[K  {S.MUTED}TPS: {tps:.1f}{S.R}")
                    sys.stdout.flush()

        # A tail that never grew into a tag is just text after all.
        for kind, text in stream.close():
            if kind == "think":
                thinking_text += text
                if config.SHOW_THINKING and thought_open:
                    emit_thought(text)
            else:
                close_thought()
                in_code = emit(text, in_code)
        close_thought()

        if printing:
            sys.stdout.write('\r\033[K')
            sys.stdout.flush()
        if line_buffer:
            in_code = process_line(line_buffer, in_code)
        flush_table()

    finally:
        if not spin_task.done():
            spin_task.cancel()
            try: await spin_task
            except asyncio.CancelledError: pass
            
    config.token_history.append({"prompt": prompt_tokens, "completion": completion_tokens})
    
    if not stream.saw_tool_call:
        _fmt_tokens(prompt_tokens, completion_tokens, total_duration, eval_duration)
        
    return full_text


_CLOSERS = {"{": "}", "[": "]"}


def _json_tail_state(blob: str) -> tuple[list[str], bool]:
    """Walk a JSON fragment and report (unclosed brackets, still inside a string)."""
    depth: list[str] = []
    in_string = False
    escaped = False
    for char in blob:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _CLOSERS:
            depth.append(char)
        elif char in ("}", "]"):
            if depth:
                depth.pop()
    return depth, in_string


def _close_brackets(blob: str) -> dict | None:
    """Recover a tool call whose brackets the model failed to close.

    Small models are good at escaping a long payload and then miscounting the
    braces around it - one missing `}` at the very end throws away a whole
    generation.
    """
    depth, _ = _json_tail_state(blob)
    if not depth:
        return None
    closers = "".join(_CLOSERS[bracket] for bracket in reversed(depth))
    try:
        repaired, _ = json.JSONDecoder().raw_decode(blob + closers)
    except json.JSONDecodeError:
        return None
    return repaired if isinstance(repaired, dict) else None


# Parameters that carry a whole file or document, where the model has to escape
# hundreds of quotes and backslashes correctly - and often does not.
_LONG_TEXT_KEYS = ("content", "new_content", "old_content", "text", "body",
                   "payload", "stdin")

_JSON_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b",
                 "f": "\f", "n": "\n", "r": "\r", "t": "\t"}

_FIELD_PATTERN = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"')
_SCALAR_PATTERN = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _lenient_unescape(raw: str) -> str:
    """Undo JSON escaping, keeping anything that is not a JSON escape verbatim."""
    out = []
    i = 0
    while i < len(raw):
        char = raw[i]
        if char != "\\" or i + 1 >= len(raw):
            out.append(char)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in _JSON_ESCAPES:
            out.append(_JSON_ESCAPES[nxt])
            i += 2
        elif nxt == "u" and i + 6 <= len(raw):
            try:
                out.append(chr(int(raw[i + 2:i + 6], 16)))
                i += 6
            except ValueError:
                out.append(char)
                i += 1
        else:
            # `\}` and friends are not JSON escapes; the model meant both
            # characters literally, so keep them.
            out.append(char)
            out.append(nxt)
            i += 2
    return "".join(out)


def _lenient_tool_call(blob: str) -> dict | None:
    """Rebuild a call whose long text payload broke the JSON around it.

    Models leave a bare `"` inside the file they are writing, or write an
    invalid escape, and the value ends early. The structure is still readable:
    the short parameters sit before the payload, and the payload runs to the
    last quote.

    Only attempted when exactly one long-text parameter is present and it is the
    final field - with two of them (`edit_file`) there is no way to tell where
    the first was meant to end, and guessing could corrupt a file.
    """
    name_match = re.search(r'"name"\s*:\s*"([A-Za-z0-9_]+)"', blob)
    if not name_match:
        return None

    long_fields = [(m.group(1), m.end()) for m in _FIELD_PATTERN.finditer(blob)
                   if m.group(1) in _LONG_TEXT_KEYS]
    if len(long_fields) != 1:
        return None
    key, value_start = long_fields[0]

    close_quote = blob.rfind('"')
    if close_quote <= value_start:
        return None
    if blob[close_quote + 1:].strip(" \t\r\n}]),"):
        return None          # something other than brackets follows: not the last field

    arguments = {}
    for match in _SCALAR_PATTERN.finditer(blob[:value_start]):
        if match.group(1) != "name":
            arguments[match.group(1)] = _lenient_unescape(match.group(2))
    arguments[key] = _lenient_unescape(blob[value_start:close_quote])

    return {"name": name_match.group(1), "arguments": arguments}


def _normalise_arguments(tool_data: dict) -> dict:
    """Take the parameters from wherever the model actually put them.

    The prompt asks for them nested under `arguments`, but models routinely
    flatten them to the top level next to `name`. Reading only `arguments` then
    hands the tool an empty dict - which is why `write_file` was failing with
    "No such file or directory: ''".
    """
    arguments = tool_data.get("arguments")
    if isinstance(arguments, dict) and arguments:
        return arguments
    flattened = {key: value for key, value in tool_data.items()
                 if key not in ("name", "arguments")}
    if flattened:
        return flattened
    return arguments if isinstance(arguments, dict) else {}


# A long text parameter may be sent as a raw block after the JSON instead of
# inside it. Escaping a whole source file into a JSON string is the single thing
# small models get wrong most often - a bare quote, a lost backslash before a
# line continuation, one uncounted brace - and this removes the need entirely.
_RAW_BLOCK_PATTERN = re.compile(
    r"<(content|new_content|old_content|text|body|stdin)>\r?\n?(.*?)\r?\n?</\1>",
    re.DOTALL)


_RAW_OPEN_PATTERN = re.compile(r"<(content|new_content|old_content|text|body|stdin)>\r?\n?")


def _take_raw_blocks(blob: str, complete: bool) -> tuple[str, dict, bool]:
    """Pull `<content>…</content>` style blocks out of a tool call.

    Returns the remaining JSON, the blocks, and whether a block was left open by
    a reply that stopped early - in which case the call must be thrown away. The
    JSON around it parses perfectly well on its own, and running it would write
    a file with no content at all.
    """
    blocks = {}

    def _collect(match):
        # An empty block is nothing to go on; keep whatever the JSON had.
        if match.group(2).strip():
            blocks[match.group(1)] = match.group(2)
        return ""

    stripped = _RAW_BLOCK_PATTERN.sub(_collect, blob)

    opened = _RAW_OPEN_PATTERN.search(stripped)
    if opened:
        if not complete:
            return stripped, blocks, True
        # The model closed `</tool_call>` but forgot `</content>`: the text is
        # all there, so take it to the end.
        blocks[opened.group(1)] = stripped[opened.end():].rstrip()
        stripped = stripped[:opened.start()]

    return stripped, blocks, False


def _decode_tool_call(blob: str, quiet: bool, complete: bool = True) -> tuple[str, dict] | None:
    blob, raw_blocks, cut_block = _take_raw_blocks(blob.strip(), complete)
    blob = blob.strip()
    note = ""

    if cut_block:
        if not quiet:
            print(f"  {S.ERR}✗ The tool call was cut off before it finished; "
                  f"nothing was run.{S.R}")
            print(f"  {S.MUTED}  The reply probably hit num_predict "
                  f"({config.NUM_PREDICT}). Raise it in config.py.{S.R}")
        return None

    try:
        # raw_decode stops at the end of the first JSON value, so a missing or
        # duplicated closing tag does not matter.
        tool_data, _ = json.JSONDecoder().raw_decode(blob)
        if not isinstance(tool_data, dict):
            tool_data = None
    except json.JSONDecodeError:
        tool_data = None

    depth, in_string = _json_tail_state(blob)
    if tool_data is None and not in_string and complete:
        # Repair only what the model finished writing. If it stopped mid-string,
        # or never closed the tag, the payload is genuinely incomplete and
        # closing the brackets would invent arguments that were never sent.
        tool_data = _close_brackets(blob)
        if tool_data is not None:
            note = f"{len(depth)} bracket(s) were left unclosed"
        else:
            tool_data = _lenient_tool_call(blob)
            if tool_data is not None:
                note = "the payload broke the JSON around it and was read by position"

    if tool_data is None:
        if not quiet:
            if in_string or not complete:
                print(f"  {S.ERR}✗ The tool call was cut off before it finished; "
                      f"nothing was run.{S.R}")
                print(f"  {S.MUTED}  The reply probably hit num_predict "
                      f"({config.NUM_PREDICT}). Raise it in config.py.{S.R}")
            else:
                print(f"  {S.ERR}✗ AI generated invalid JSON for the tool call.{S.R}")
        return None

    if not tool_data.get("name"):
        if not quiet:
            print(f"  {S.ERR}✗ The tool call is missing a tool name.{S.R}")
        return None

    arguments = _normalise_arguments(tool_data)
    if not isinstance(tool_data.get("arguments"), dict) or (
            not tool_data.get("arguments") and arguments):
        note = (note + "; " if note else "") + "the parameters were not nested under 'arguments'"
    arguments.update(raw_blocks)          # a raw block always wins over the JSON
    if note and not quiet:
        print(f"  {S.WARN}⚠ Malformed tool call: {note}. Repaired and continuing.{S.R}")
    return tool_data["name"], arguments


def parse_tool_calls(response_text: str, quiet: bool = False) -> list[tuple[str, dict]]:
    """Every tool call in a response, in the order the model wrote them.

    Models routinely ask for two or three things at once. Reading only the first
    threw the rest away and cost a whole extra round trip each - expensive when
    the model is running on your own GPU.
    """
    calls = []
    closed_block = False
    for match in re.finditer(r"<tool_call>(.*?)</tool_call>", response_text, re.DOTALL):
        closed_block = True
        call = _decode_tool_call(match.group(1), quiet)
        if call:
            calls.append(call)
    if calls or closed_block:
        # A properly closed block was already judged on its own merits; retrying
        # it as an unterminated one would only report the same failure twice.
        return calls

    start = response_text.find(TOOL_CALL_TAG)
    if start == -1:
        return []
    # The model opened a tool call and never closed it. The tag is hidden from
    # the user either way, so read the JSON that follows rather than letting the
    # turn die silently.
    call = _decode_tool_call(response_text[start + len(TOOL_CALL_TAG):], quiet, complete=False)
    return [call] if call else []


MAX_PARSE_RETRIES = 2


async def chat_turn(messages: list[dict]) -> str:
    call_count = 0
    parse_failures = 0
    while True:
        # Text a console mangled cannot be encoded, so one bad character would
        # fail this request and every later one. Repair it before it is sent.
        config.repair_messages(messages)
        if call_count > 0 and call_count % config.MAX_TOOL_CALLS == 0:
            print(f"\n  {S.WARN}⚠  Tool call limit ({config.MAX_TOOL_CALLS}) reached.{S.R}")
            try:
                user_choice = input(f"  {S.WARN}Continue? {S.MUTED}[{S.OK}y{S.MUTED}/{S.ERR}n{S.MUTED}]{S.R} {S.WARN}›{S.R} ").strip().lower()
            except:
                user_choice = "n"
            if user_choice != 'y':
                return messages[-2]["content"] if len(messages) >= 2 else "Tool usage stopped."
                
        response_text = await stream_reply(messages)
        stored = response_text if config.STORE_THINKING else strip_thinking(response_text)
        messages.append({"role": "assistant", "content": stored})

        parsed = parse_tool_calls(response_text)
        if not parsed:
            if TOOL_CALL_TAG in response_text and parse_failures < MAX_PARSE_RETRIES:
                # The model meant to call a tool and produced something we could
                # not read. Say so instead of ending the turn in silence - the
                # model can correct itself, and usually does.
                parse_failures += 1
                messages.append({"role": "user", "content": (
                    "[Tool Error]: Your last <tool_call> could not be parsed. Send it "
                    "again as ONE JSON object of exactly this shape:\n"
                    '{"name": "<tool name>", "arguments": {"<param>": "<value>"}}\n'
                    "Every parameter goes inside \"arguments\". Inside a string value, "
                    "write \\\" for a quote, \\\\ for a backslash and \\n for a line break, "
                    "and close every brace you opened.")})
                continue
            return stored

        parse_failures = 0

        for function_name, arguments in parsed:
            tool_result = dispatch_tool(function_name, arguments)
            if tool_result is None:
                # An unknown tool used to end the turn in silence. Hand the
                # model the error so it can correct itself instead.
                tool_result = (f"[Error] There is no tool named '{function_name}'. "
                               "Use only the tools listed in your system prompt.")

            _fmt_tool_result(function_name, tool_result)

            messages.append({
                "role": "user",
                "content": f"[Tool Result for '{function_name}']:\n{tool_result}",
            })
            call_count += 1

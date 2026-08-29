import sys
import re
import json
import asyncio
import itertools
import time
import random
import ollama
import config
from config import S
from tui import _fmt_tool_call, _fmt_tool_result, _fmt_tokens
from renderer import _render_line, _format_table, _render_full
from tools import dispatch_tool


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


async def call_ollama(client: ollama.AsyncClient, messages: list[dict]) -> str:

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
        response_stream = await client.chat(model=config.MODEL, messages=messages, stream=True, options={"num_ctx": config.NUM_CTX, "num_predict": config.NUM_PREDICT})

        async for chunk in response_stream:
            if start_time is None:
                start_time = time.time()

            chunk_count += 1

            message = chunk.get('message') or {}
            content = message.get('content', '') or ''
            full_text += content

            if chunk.get('done'):
                prompt_tokens = chunk.get("prompt_eval_count", 0)
                completion_tokens = chunk.get("eval_count", 0)
                total_duration = chunk.get("total_duration", 0) / 1e9
                eval_duration = chunk.get("eval_duration", 0) / 1e9

            # Newer Ollama builds hand reasoning back in its own field instead
            # of wrapping it in tags. It never enters `full_text`, so it stays
            # out of the conversation history for free.
            events = []
            native_thinking = message.get('thinking', '') or ''
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


def _decode_tool_call(blob: str, quiet: bool) -> tuple[str, dict] | None:
    try:
        # raw_decode stops at the end of the first JSON value, so a missing or
        # duplicated closing tag does not matter.
        tool_data, _ = json.JSONDecoder().raw_decode(blob.strip())
    except json.JSONDecodeError:
        if not quiet:
            print(f"  {S.ERR}✗ AI generated invalid JSON for the tool call.{S.R}")
        return None

    if not isinstance(tool_data, dict) or not tool_data.get("name"):
        if not quiet:
            print(f"  {S.ERR}✗ The tool call is missing a tool name.{S.R}")
        return None
    return tool_data["name"], tool_data.get("arguments", {})


def parse_tool_calls(response_text: str, quiet: bool = False) -> list[tuple[str, dict]]:
    """Every tool call in a response, in the order the model wrote them.

    Models routinely ask for two or three things at once. Reading only the first
    threw the rest away and cost a whole extra round trip each - expensive when
    the model is running on your own GPU.
    """
    calls = []
    for match in re.finditer(r"<tool_call>(.*?)</tool_call>", response_text, re.DOTALL):
        call = _decode_tool_call(match.group(1), quiet)
        if call:
            calls.append(call)
    if calls:
        return calls

    start = response_text.find(TOOL_CALL_TAG)
    if start == -1:
        return []
    # The model opened a tool call and never closed it. The tag is hidden from
    # the user either way, so read the JSON that follows rather than letting the
    # turn die silently.
    call = _decode_tool_call(response_text[start + len(TOOL_CALL_TAG):], quiet)
    return [call] if call else []


async def chat_turn(client: ollama.AsyncClient, messages: list[dict]) -> str:
    call_count = 0
    while True:
        if call_count > 0 and call_count % config.MAX_TOOL_CALLS == 0:
            print(f"\n  {S.WARN}⚠  Tool call limit ({config.MAX_TOOL_CALLS}) reached.{S.R}")
            try:
                user_choice = input(f"  {S.WARN}Continue? {S.MUTED}[{S.OK}y{S.MUTED}/{S.ERR}n{S.MUTED}]{S.R} {S.WARN}›{S.R} ").strip().lower()
            except:
                user_choice = "n"
            if user_choice != 'y':
                return messages[-2]["content"] if len(messages) >= 2 else "Tool usage stopped."
                
        response_text = await call_ollama(client, messages)
        stored = response_text if config.STORE_THINKING else strip_thinking(response_text)
        messages.append({"role": "assistant", "content": stored})

        parsed = parse_tool_calls(response_text)
        if not parsed:
            return stored

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

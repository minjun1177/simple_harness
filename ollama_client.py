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
    is_tool_call_check_done = False
    is_tool_call = False
    
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

            content = chunk['message'].get('content', '')
            full_text += content

            if chunk.get('done'):
                prompt_tokens = chunk.get("prompt_eval_count", 0)
                completion_tokens = chunk.get("eval_count", 0)
                total_duration = chunk.get("total_duration", 0) / 1e9
                eval_duration = chunk.get("eval_duration", 0) / 1e9

            if not is_tool_call_check_done:
                if "<tool_call>" in full_text:
                    is_tool_call = True
                    is_tool_call_check_done = True
                elif len(full_text) > 15 or "\n" in full_text:
                    is_tool_call = False
                    is_tool_call_check_done = True
                    if not spin_task.done():
                        spin_task.cancel()
                        sys.stdout.write('\r\033[K')
                    for char in full_text:
                        if char == '\n':
                            in_code = process_line(line_buffer, in_code)
                            line_buffer = ""
                        else:
                            line_buffer += char
                continue

            if not is_tool_call:
                for char in content:
                    if char == '\n':
                        sys.stdout.write('\r\033[K')
                        sys.stdout.flush()
                        in_code = process_line(line_buffer, in_code)
                        line_buffer = ""
                    else:
                        line_buffer += char

                elapsed = time.time() - start_time
                if elapsed > 0.1:
                    tps = chunk_count / elapsed
                    sys.stdout.write(f"\r\033[K  {S.MUTED}TPS: {tps:.1f}{S.R}")
                    sys.stdout.flush()

        if not is_tool_call:
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
    
    if not is_tool_call:
        _fmt_tokens(prompt_tokens, completion_tokens, total_duration, eval_duration)
        
    return full_text


def parse_tool_call(response_text: str) -> tuple[str, dict] | None:
    match = re.search(r"<tool_call>(.*?)</tool_call>", response_text, re.DOTALL)
    if not match: return None
    try:
        tool_data = json.loads(match.group(1))
        name = tool_data.get("name")
        arguments = tool_data.get("arguments", {})
        if name: return name, arguments
    except json.JSONDecodeError:
        print(f"  {S.ERR}✗ AI generated invalid JSON for the tool call.{S.R}")
    return None


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
        messages.append({"role": "assistant", "content": response_text})

        parsed = parse_tool_call(response_text)
        if parsed is None:
            return response_text

        function_name, arguments = parsed
        tool_result = dispatch_tool(function_name, arguments)

        if tool_result is None:
            return response_text

        _fmt_tool_result(function_name, tool_result)

        messages.append({
            "role": "user",
            "content": f"[Tool Result for '{function_name}']:\n{tool_result}",
        })
        call_count += 1

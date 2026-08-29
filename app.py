import asyncio
import sys
import os
import re
import datetime
import ollama
import config
import skills
import mcp_client
import permissions
from config import S
from systemprompt import systemprompt as _build_system_prompt
from tui import _welcome, _show_help, _show_skills, _show_mcp, _show_perms, _fmt_tool_call, _fmt_tool_result, display_usage_graph, _hr
from renderer import _render_full
from session import (save_session, load_session, list_sessions, find_sessions,
                     rename_session, generate_session_title, clean_title)
from context import manage_context
from ollama_client import chat_turn, parse_tool_calls, strip_thinking


def _compose_system_prompt(summary: str = "") -> str:
    base = config.SYSTEM_PROMPT
    if config.CUSTOM_PERSONA:
        base = config.CUSTOM_PERSONA + "\n\n" + base
    return base + summary


def _extract_summary(system_content: str) -> str:
    m = re.search(r'\n\n<SUMMARY>(.*?)</SUMMARY>', system_content, re.DOTALL)
    return f"\n\n<SUMMARY>{m.group(1)}</SUMMARY>" if m else ""


def _refresh_system_prompt(messages: list[dict]) -> None:
    """Rebuild the system message in place, keeping persona and summary intact."""
    summary = _extract_summary(messages[0]["content"])
    config.SYSTEM_PROMPT = _build_system_prompt()
    messages[0]["content"] = _compose_system_prompt(summary)


def _connect_mcp_servers() -> list:
    """Bring up the configured MCP servers. Returns the ones that failed.

    This runs before the first system prompt is built, because the servers'
    tool lists are part of it.
    """
    if not config.MCP_ENABLED:
        return []
    pending = [s for s in mcp_client.load_servers().values() if s.state != "disabled"]
    if not pending:
        return []

    plural = "s" if len(pending) != 1 else ""
    sys.stdout.write(f"  {S.MUTED}⟳ connecting {len(pending)} MCP server{plural}…{S.R}")
    sys.stdout.flush()
    try:
        mcp_client.connect_all()
    finally:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
    return [s for s in pending if s.state == "failed"]


def _report_mcp_problems(failed: list) -> None:
    for problem in getattr(mcp_client.load_servers, "errors", []):
        print(f"  {S.ERR}✗ {problem}{S.R}")
    for server in failed:
        print(f"  {S.WARN}⚠ MCP server '{server.name}' failed: {server.error[:200]}{S.R}")
    if failed:
        print(f"  {S.MUTED}  Run {S.ACCENT}/mcp{S.MUTED} for details, {S.ACCENT}/mcp reload{S.MUTED} to retry.{S.R}\n")


async def main() -> None:
    client = ollama.AsyncClient()

    if config.CURRENT_OS == "Windows":
        os.system("")

    print("\033[2J\033[H", end="")
    failed_mcp = _connect_mcp_servers()
    config.SYSTEM_PROMPT = _build_system_prompt()
    messages: list[dict] = [{"role": "system", "content": _compose_system_prompt()}]

    _welcome()
    _report_mcp_problems(failed_mcp)

    current_session_id = None

    if config.PROMPT_TOOLKIT_AVAILABLE:
        from config import SlashCommandCompleter, PromptSession, FileHistory, ANSI
        completer = SlashCommandCompleter(['/help', '/clear', '/usage', '/model', '/models', '/exit', '/quit', '/sessions', '/load', '/title', '/autotitle', '/automode', '/fullcontent', '/record', '/export', '/system', '/planmode', '/skills', '/skill', '/mcp', '/perms', '/think'])
        session_pt = PromptSession(
            history=FileHistory('.chat_history'),
            completer=completer,
        )

    while True:
        try:
            if config.PROMPT_TOOLKIT_AVAILABLE:
                user_input = await session_pt.prompt_async(ANSI(f"  {S.USER_CLR}{S.BOLD}❯{S.R} "))
                user_input = user_input.strip()
            else:
                user_input = input(f"  {S.USER_CLR}{S.BOLD}❯{S.R} ").strip()
        except (EOFError, KeyboardInterrupt):
            mcp_client.shutdown()
            print(f"\n\n  {S.GRAY}Goodbye!{S.R}\n")
            break

        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("/exit", "/quit"):
            mcp_client.shutdown()
            print(f"\n  {S.GRAY}Goodbye!{S.R}\n")
            break
        if cmd == "/usage":
            display_usage_graph(messages)
            continue
        if cmd == "/help":
            _show_help()
            continue
        if cmd == "/clear":
            config.SYSTEM_PROMPT = _build_system_prompt()
            messages = [{"role": "system", "content": _compose_system_prompt()}]
            current_session_id = None
            config.SESSION_TITLE = ""
            config.token_history.clear()
            config.LOADED_SKILLS.clear()
            print("\033[2J\033[H", end="")
            _welcome()
            print(f"  {S.OK}✓ Conversation and usage cleared.{S.R}\n")
            continue
        if cmd == "/models":
            print(f"\n  {S.BOLD}{S.ACCENT}Available Ollama Models{S.R}")
            print(f"  {_hr(width=50)}")
            try:
                model_list = ollama.list()
                models_available = model_list.get("models", []) if isinstance(model_list, dict) else (model_list.models if hasattr(model_list, 'models') else [])
                if not models_available:
                    print(f"  {S.WARN}⚠ No models found. Pull a model with: ollama pull <model>{S.R}")
                else:
                    for i, m in enumerate(models_available, 1):
                        name = m.get("model", m.get("name", "unknown")) if isinstance(m, dict) else (m.model if hasattr(m, 'model') else str(m))
                        size_bytes = m.get("size", 0) if isinstance(m, dict) else (m.size if hasattr(m, 'size') else 0)
                        size_gb = size_bytes / (1024**3)
                        marker = f" {S.OK}◀ current{S.R}" if name == config.MODEL else ""
                        if size_gb >= 1:
                            print(f"  {S.ACCENT}{i:3}.{S.R} {S.WHITE}{name}{S.R}  {S.GRAY}({size_gb:.1f}GB){S.R}{marker}")
                        else:
                            size_mb = size_bytes / (1024**2)
                            print(f"  {S.ACCENT}{i:3}.{S.R} {S.WHITE}{name}{S.R}  {S.GRAY}({size_mb:.0f}MB){S.R}{marker}")
            except Exception as e:
                print(f"  {S.ERR}✗ Failed to list models: {e}{S.R}")
            print()
            continue
        if cmd == "/model":
            print(f"\n  {S.GRAY}model{S.R}  {S.WHITE}{config.MODEL}{S.R}\n")
            try:
                model_list = ollama.list()
                models_available = model_list.get("models", []) if isinstance(model_list, dict) else (model_list.models if hasattr(model_list, 'models') else [])
                if not models_available:
                    print(f"  {S.WARN}⚠ No models found.{S.R}\n")
                    continue
                model_names = []
                for m in models_available:
                    name = m.get("model", m.get("name", "unknown")) if isinstance(m, dict) else (m.model if hasattr(m, 'model') else str(m))
                    model_names.append(name)
                print(f"  {S.BOLD}{S.ACCENT}Select a model:{S.R}")
                for i, name in enumerate(model_names, 1):
                    marker = f" {S.OK}◀ current{S.R}" if name == config.MODEL else ""
                    print(f"  {S.ACCENT}{i:3}.{S.R} {S.WHITE}{name}{S.R}{marker}")
                print(f"  {S.MUTED}  0.{S.R} {S.GRAY}Cancel{S.R}")
                print()
                try:
                    choice = input(f"  {S.INFO}Select{S.R} {S.MUTED}(0~{len(model_names)}){S.R} {S.INFO}›{S.R} ").strip()
                    if not choice or choice == "0":
                        print(f"  {S.GRAY}Cancelled.{S.R}\n")
                        continue
                    idx = int(choice) - 1
                    if 0 <= idx < len(model_names):
                        old_model = config.MODEL
                        config.MODEL = model_names[idx]
                        if old_model == config.MODEL:
                            print(f"  {S.GRAY}Already using {config.MODEL}.{S.R}\n")
                        else:
                            print(f"  {S.OK}✓ Model changed: {old_model} → {config.MODEL}{S.R}\n")
                    else:
                        print(f"  {S.ERR}✗ Invalid selection.{S.R}\n")
                except (ValueError, EOFError, KeyboardInterrupt):
                    print(f"\n  {S.GRAY}Cancelled.{S.R}\n")
            except Exception as e:
                print(f"  {S.ERR}✗ Failed to list models: {e}{S.R}\n")
            continue
        if cmd == "/sessions":
            sessions = list_sessions()
            print(f"\n  {S.BOLD}{S.ACCENT}Saved Sessions{S.R}")
            if not sessions:
                print(f"  {S.GRAY}  No saved sessions yet.{S.R}")
            for sid, title, meta in sessions:
                marker = f" {S.OK}◀ current{S.R}" if sid == current_session_id else ""
                print(f"  {S.GRAY}•{S.R} {S.WHITE}{title or S.GRAY + '(untitled)' + S.R}{S.R}{marker}")
                print(f"    {S.MUTED}{sid}{S.R}  {S.GRAY}{meta}{S.R}")
            print(f"\n  {S.GRAY}Load one with {S.ACCENT}/load <id or title>{S.GRAY}.{S.R}\n")
            continue
        if cmd.startswith("/load"):
            parts = user_input.split(" ", 1)
            if len(parts) < 2 or not parts[1].strip():
                print(f"  {S.ERR}✗ Usage: /load <id or title>{S.R}\n")
                continue
            query = parts[1].strip()
            matches = find_sessions(query)
            if len(matches) > 1:
                print(f"  {S.WARN}⚠ '{query}' matches {len(matches)} sessions:{S.R}")
                for m_sid, m_title, _ in matches[:10]:
                    print(f"  {S.GRAY}•{S.R} {S.WHITE}{m_title or '(untitled)'}{S.R}  {S.MUTED}{m_sid}{S.R}")
                print(f"  {S.GRAY}Re-run /load with one of the ids above.{S.R}\n")
                continue
            sid = matches[0][0] if matches else query
            loaded = load_session(sid)
            if loaded:
                if isinstance(loaded, dict) and loaded.get("version") in (2, 3):
                    messages = loaded.get("messages", [])
                    config.token_history.clear()
                    config.token_history.extend(loaded.get("token_history", []))
                    config.MODEL = loaded.get("model", config.MODEL)
                    config.CUSTOM_PERSONA = loaded.get("persona", config.CUSTOM_PERSONA)
                    config.SESSION_TITLE = loaded.get("title", "")
                else:
                    messages = loaded
                    config.token_history.clear()
                    config.SESSION_TITLE = ""
                current_session_id = sid
                config.LOADED_SKILLS[:] = skills.loaded_skill_names(messages)
                label = config.SESSION_TITLE or sid
                print(f"  {S.OK}✓ Loaded session: {label} (Model: {config.MODEL}){S.R}\n")
                for msg in messages:
                    if msg["role"] == "system": 
                        continue
                    elif msg["role"] == "user":
                        if msg["content"].startswith("[Tool Result for '"):
                            m = re.match(r"\[Tool Result for '([^']+)'\]:\n(.*)", msg["content"], re.DOTALL)
                            if m:
                                _fmt_tool_result(m.group(1), m.group(2))
                            continue
                        print(f"  {S.USER_CLR}{S.BOLD}❯{S.R} {msg['content']}")
                    elif msg["role"] == "assistant":
                        for name, arguments in parse_tool_calls(msg["content"], quiet=True):
                            _fmt_tool_call(name, arguments)

                        c = re.sub(r'<tool_call>.*?</tool_call>', '', msg["content"], flags=re.DOTALL)
                        c = strip_thinking(c)
                        if c:
                            print(_render_full(c))
                            print()
            else:
                print(f"  {S.ERR}✗ Session not found: {sid}{S.R}\n")
            continue
        if cmd == "/title" or cmd.startswith("/title "):
            parts = user_input.split(" ", 1)
            new_title = parts[1].strip() if len(parts) > 1 else ""
            if not new_title:
                shown = config.SESSION_TITLE or f"{S.GRAY}(untitled){S.R}"
                print(f"\n  {S.GRAY}title{S.R}  {S.WHITE}{shown}{S.R}")
                print(f"  {S.GRAY}id{S.R}     {S.WHITE}{current_session_id or '(not saved yet)'}{S.R}")
                print(f"  {S.MUTED}Rename with /title <new title>{S.R}\n")
                continue
            if not clean_title(new_title):
                print(f"  {S.ERR}✗ That title is empty after cleanup.{S.R}\n")
                continue
            current_session_id = rename_session(current_session_id, new_title)
            print(f"  {S.OK}✓ Session titled: {config.SESSION_TITLE}{S.R} {S.MUTED}({current_session_id or 'saved on next message'}){S.R}\n")
            continue
        if cmd.startswith("/autotitle"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2 or parts[1] not in ("on", "off"):
                print(f"  {S.ERR}✗ Usage: /autotitle <on/off>  (currently {'on' if config.AUTO_TITLE else 'off'}){S.R}\n")
                continue
            config.AUTO_TITLE = parts[1] == "on"
            print(f"  {S.INFO}✓ Auto session titling is {'ON' if config.AUTO_TITLE else 'OFF'}.{S.R}")
            continue
        if cmd.startswith("/automode"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2 or not parts[1] in ("on", "off"):
                print(f"  {S.ERR}✗ Usage: /automode <on/off>{S.R}\n")
                continue
            if parts[1] == "on":
                config.AUTO_ALLOW = True
            if parts[1] == "off":
                config.AUTO_ALLOW = False
            print(f"  {S.INFO}✓ Automode has been on.{S.R}" if parts[1] == "on" else f"  {S.INFO}✓ Automode has been off.{S.R}")
            continue
        if cmd.startswith("/fullcontent"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2 or not parts[1] in ("on", "off"):
                print(f"  {S.ERR}✗ Usage: /fullcontent <on/off>{S.R}\n")
                continue
            if parts[1] == "on":
                config.RETURN_ALL_FILE_CONTENT = True
            if parts[1] == "off":
                config.RETURN_ALL_FILE_CONTENT = False
            print(f"  {S.INFO}✓ Full content mode has been turned on.{S.R}" if parts[1] == "on" else f"  {S.INFO}✓ Full content mode has been turned off.{S.R}")
            continue
        if cmd.startswith("/record"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2 or not parts[1] in ("on", "off"):
                print(f"  {S.ERR}✗ Usage: /record <on/off>{S.R}\n")
                continue
            if parts[1] == "on":
                config.SAVE_CHAT_HISTORY = True
            if parts[1] == "off":
                config.SAVE_CHAT_HISTORY = False
            print(f"  {S.INFO}✓ Chat history recording is ON.{S.R}" if parts[1] == "on" else f"  {S.INFO}✓ Chat history recording is OFF.{S.R}")
            continue
        if cmd.startswith("/export"):
            parts = cmd.split(" ", 1)
            filename = parts[1].strip() if len(parts) > 1 else f"export_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    for m in messages:
                        if m["role"] == "system": continue
                        role_name = "User" if m["role"] == "user" else "Assistant"
                        c = re.sub(r'<tool_call>.*?</tool_call>', '', m["content"], flags=re.DOTALL)
                        c = strip_thinking(c)
                        if c: f.write(f"### {role_name}\n\n{c}\n\n")
                print(f"  {S.OK}✓ Conversation exported to {filename}{S.R}\n")
            except Exception as e:
                print(f"  {S.ERR}✗ Export failed: {e}{S.R}\n")
            continue
        if cmd.startswith("/system"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2:
                print(f"  {S.ERR}✗ Usage: /system <new prompt> or /system reset{S.R}\n")
                continue
            new_prompt = parts[1].strip()
            current_sys = messages[0]["content"]
            summary_match = re.search(r'\n\n<SUMMARY>(.*?)</SUMMARY>', current_sys, re.DOTALL)
            summary_text = f"\n\n<SUMMARY>{summary_match.group(1)}</SUMMARY>" if summary_match else ""

            if new_prompt.lower() == "reset":
                config.CUSTOM_PERSONA = ""
                messages[0]["content"] = config.SYSTEM_PROMPT + summary_text
                print(f"  {S.INFO}✓ System prompt reset to default.{S.R}")
                print(f"  {S.WARN}⚠ If the persona context from the previous conversation remains, please clear the conversation history with /clear.{S.R}\n")
            else:
                config.CUSTOM_PERSONA = new_prompt
                messages[0]["content"] = config.CUSTOM_PERSONA + "\n\n" + config.SYSTEM_PROMPT + summary_text
                print(f"  {S.INFO}✓ System prompt updated.{S.R}")
                print(f"  {S.WARN}⚠ To ensure the persona is applied correctly, please clear the previous conversation with /clear.{S.R}\n")
            continue
        if cmd.startswith("/planmode"):
            parts = cmd.split(" ", 1)
            if len(parts) < 2 or parts[1] not in ("on", "off"):
                print(f"  {S.ERR}✗ Usage: /planmode <on/off>{S.R}\n")
                continue
            config.PLANMODE = True if parts[1] == "on" else False
            print(f"  {S.INFO}✓ Plan mode is {'ON' if config.PLANMODE else 'OFF'}.{S.R}")
            continue
        if cmd == "/skills" or cmd.startswith("/skills "):
            arg = user_input.split(" ", 1)[1].strip().lower() if " " in user_input else ""
            if not arg:
                _show_skills()
            elif arg == "reload":
                skills.discover_skills(force=True)
                _refresh_system_prompt(messages)
                print(f"  {S.OK}✓ Skills reloaded: {len(skills.list_skills())} available.{S.R}\n")
            else:
                print(f"  {S.ERR}✗ Usage: /skills [reload]{S.R}\n")
            continue
        if cmd.startswith("/skill"):
            parts = user_input.split(" ", 1)
            if len(parts) < 2 or not parts[1].strip():
                print(f"  {S.ERR}✗ Usage: /skill <name>  (see /skills){S.R}\n")
                continue
            skill = skills.get_skill(parts[1].strip())
            if skill is None:
                print(f"  {S.ERR}✗ Skill not found: {parts[1].strip()}{S.R}\n")
                continue
            _fmt_tool_call("use_skill", {"skill_name": skill["name"]})
            result = skills.handle_use_skill(skill["name"])
            _fmt_tool_result("use_skill", result)
            messages.append({"role": "user", "content": f"[Tool Result for 'use_skill']:\n{result}"})
            current_session_id = save_session(messages, current_session_id)
            continue
        if cmd == "/mcp" or cmd.startswith("/mcp "):
            parts = user_input.split()
            sub = parts[1].lower() if len(parts) > 1 else ""
            args = parts[2:]
            injected = ""

            if not sub:
                _show_mcp()
            elif sub in ("tools", "prompts", "all"):
                _show_mcp(sub, args[0] if args else "")
            elif sub in ("reload", "refresh"):
                sys.stdout.write(f"  {S.MUTED}⟳ reloading MCP servers…{S.R}")
                sys.stdout.flush()
                mcp_client.reconnect()
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                _refresh_system_prompt(messages)
                print(f"  {S.OK}✓ MCP reloaded: {mcp_client.status_summary()}.{S.R}\n")
                _report_mcp_problems([s for s in mcp_client.all_servers() if s.state == "failed"])
            elif sub in ("connect", "reconnect"):
                if not args:
                    print(f"  {S.ERR}✗ Usage: /mcp connect <server name>{S.R}\n")
                else:
                    touched = mcp_client.reconnect(args[0])
                    if not touched:
                        print(f"  {S.ERR}✗ No MCP server named '{args[0]}'. See /mcp.{S.R}\n")
                    else:
                        server = touched[0]
                        _refresh_system_prompt(messages)
                        if server.state == "connected":
                            print(f"  {S.OK}✓ '{server.name}' connected: {len(server.tools)} tool(s).{S.R}\n")
                        else:
                            print(f"  {S.ERR}✗ '{server.name}' is {server.state}: {server.error[:200]}{S.R}\n")
            elif sub == "resources":
                print()
                print(mcp_client.list_resources_text(args[0] if args else ""))
                print()
            elif sub == "prompt":
                if len(args) < 2:
                    print(f"  {S.ERR}✗ Usage: /mcp prompt <server> <prompt name> [key=value ...]{S.R}\n")
                else:
                    server = mcp_client.get_server(args[0])
                    if server is None or server.state != "connected":
                        print(f"  {S.ERR}✗ No connected MCP server named '{args[0]}'.{S.R}\n")
                    else:
                        prompt_args = {}
                        for token in args[2:]:
                            key, sep, value = token.partition("=")
                            if sep:
                                prompt_args[key] = value
                        try:
                            fetched = server.get_prompt(args[1], prompt_args)
                        except Exception as e:
                            print(f"  {S.ERR}✗ {server.name}: {e}{S.R}\n")
                        else:
                            injected = mcp_client.prompt_to_text(fetched)
                            print(f"\n  {S.MUTED}─ prompt '{args[1]}' from {server.name}{S.R}")
                            print(_render_full(injected))
                            print()
            elif sub in ("on", "off"):
                config.MCP_ENABLED = sub == "on"
                if config.MCP_ENABLED:
                    _report_mcp_problems(_connect_mcp_servers())
                else:
                    mcp_client.shutdown()
                _refresh_system_prompt(messages)
                print(f"  {S.INFO}✓ MCP is {'ON' if config.MCP_ENABLED else 'OFF'}.{S.R}\n")
            else:
                print(f"  {S.ERR}✗ Usage: /mcp [tools|prompts|all|resources|reload|connect <name>|prompt <server> <name>|on|off]{S.R}\n")

            if not injected:
                continue
            user_input = injected
        if cmd == "/perms" or cmd.startswith("/perms "):
            parts = user_input.split(" ", 2)
            sub = parts[1].lower() if len(parts) > 1 else ""
            argument = parts[2].strip() if len(parts) > 2 else ""

            if not sub:
                _show_perms()
            elif sub in ("reload", "refresh"):
                permissions.load_rules(force=True)
                allowed = len(permissions.rules_for("allow"))
                denied = len(permissions.rules_for("deny"))
                print(f"  {S.OK}✓ Permission rules reloaded: {allowed} allow, {denied} deny.{S.R}\n")
                for problem in permissions.errors:
                    print(f"  {S.ERR}✗ {problem}{S.R}")
            elif sub in ("allow", "deny"):
                if not argument:
                    print(f"  {S.ERR}✗ Usage: /perms {sub} <rule>   e.g. /perms {sub} run_cmd(git *){S.R}\n")
                else:
                    saved, where = permissions.add_rule(argument, sub)
                    if saved:
                        print(f"  {S.OK}✓ {sub}: {argument}{S.R} {S.MUTED}({where}){S.R}\n")
                    else:
                        print(f"  {S.ERR}✗ Could not save the rule: {where}{S.R}\n")
            else:
                print(f"  {S.ERR}✗ Usage: /perms [reload|allow <rule>|deny <rule>]{S.R}\n")
            continue
        if cmd == "/think" or cmd.startswith("/think "):
            parts = cmd.split(" ", 1)
            if len(parts) < 2 or parts[1].strip() not in ("on", "off"):
                state = "shown" if config.SHOW_THINKING else "hidden"
                print(f"  {S.ERR}✗ Usage: /think <on/off>  (a model's reasoning is currently {state}){S.R}\n")
                continue
            config.SHOW_THINKING = parts[1].strip() == "on"
            print(f"  {S.INFO}✓ Model reasoning is {'SHOWN' if config.SHOW_THINKING else 'HIDDEN'}."
                  f"{S.MUTED} It is never kept in the conversation history.{S.R}\n")
            continue

        if config.PLANMODE:
            if config.AUTO_ALLOW:
                plan_prompt = (
                    "\n\n[System Note: PLAN MODE is ON. For complex tasks or file modifications, you MUST use the `submit_plan_for_approval` tool before executing changes. Since AUTOMODE is ON, it will auto-approve. Follow your blueprint and verify afterwards.]"
                )
            else:
                plan_prompt = (
                    "\n\n[System Note: PLAN MODE is ON. For complex tasks, system changes, or file modifications:\n"
                    "1. Explore the codebase using search/read tools.\n"
                    "2. You MUST call the `submit_plan_for_approval` tool to present your blueprint and wait for the tool's result.\n"
                    "3. DO NOT use edit/write/run_cmd tools until the plan is approved via the tool's return value.\n"
                    "For simple conversational queries, you may answer directly.]"
                )
            user_input += plan_prompt

        messages.append({"role": "user", "content": user_input})
        current_session_id = save_session(messages, current_session_id)

        try:
            await manage_context(client, messages)

            result = await chat_turn(client, messages)

            current_session_id = save_session(messages, current_session_id)

            # Name the session once, from the exchange that just finished.
            if config.AUTO_TITLE and not config.SESSION_TITLE and current_session_id:
                sys.stdout.write(f"  {S.MUTED}✎ naming session…{S.R}")
                sys.stdout.flush()
                title = await generate_session_title(client, messages)
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                if title:
                    current_session_id = rename_session(current_session_id, title)
                    print(f"  {S.MUTED}✎ session titled: {S.GRAY}{config.SESSION_TITLE}{S.R}\n")

        except Exception as e:
            print(f"\n  {S.ERR}✗ Error: {e}{S.R}\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n\n  {S.GRAY}Goodbye!{S.R}\n")
    except Exception as e:
        print(f"\n  {S.ERR}✗ Unexpected error: {e}{S.R}")

import argparse
import asyncio
import sys
import os
import re
import datetime
from simple_harness import __version__
from simple_harness import config
from simple_harness import paths
from simple_harness import terms
from simple_harness import channel
from simple_harness import deepthink
from simple_harness import git_ops
from simple_harness import skills
from simple_harness import mcp_client
from simple_harness import permissions
from simple_harness import providers
from simple_harness import connect
from simple_harness import mentions
from simple_harness import tools
from simple_harness import vm
from simple_harness.config import S
from simple_harness.systemprompt import systemprompt as _build_system_prompt
from simple_harness.tui import (_welcome, _show_help, _show_skills, _show_mcp, _show_perms,
                                _show_settings, _fmt_setting, _fmt_tool_call, _fmt_tool_result,
                                display_usage_graph, _hr)
from simple_harness.renderer import _render_full
from simple_harness.session import (save_session, load_session, list_sessions, find_sessions,
                     latest_in_dir, rename_session, generate_session_title, clean_title)
from simple_harness.context import manage_context
from simple_harness.llm_client import chat_turn, parse_tool_calls, strip_thinking


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


def _adopt_session(loaded) -> list[dict]:
    """Make a loaded session file the live conversation. Returns its messages.

    `/load` and `--resume` are the same act at different moments, so they are
    the same code: the model, the persona, the token history and the loaded
    skills all belong to the conversation being resumed, not to the one being
    left behind.
    """
    if isinstance(loaded, dict):
        messages = loaded.get("messages", [])
        config.token_history.clear()
        config.token_history.extend(loaded.get("token_history", []))
        config.resume_turns()
        config.MODEL = loaded.get("model", config.MODEL)
        config.CUSTOM_PERSONA = loaded.get("persona", config.CUSTOM_PERSONA)
        config.SESSION_TITLE = loaded.get("title", "")
    else:                                   # a transcript from before version 2
        messages = loaded
        config.token_history.clear()
        config.resume_turns()
        config.SESSION_TITLE = ""
    if not messages or messages[0].get("role") != "system":
        # Every later turn addresses messages[0] as the system message. A file
        # saved with none - or with none left after a repair - must not make the
        # next turn raise.
        config.SYSTEM_PROMPT = _build_system_prompt()
        messages.insert(0, {"role": "system", "content": _compose_system_prompt()})
    config.LOADED_SKILLS[:] = skills.loaded_skill_names(messages)
    return messages


def _replay_session(messages: list[dict]) -> None:
    """Print a resumed conversation the way it looked while it was happening."""
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


def _show_ambiguous(query: str, matches: list, hint: str) -> None:
    """Say which sessions a name matched, rather than picking one of them."""
    print(f"  {S.WARN}⚠ '{query}' matches {len(matches)} sessions:{S.R}")
    for sid, title, _ in matches[:10]:
        print(f"  {S.GRAY}•{S.R} {S.WHITE}{title or '(untitled)'}{S.R}  {S.MUTED}{sid}{S.R}")
    print(f"  {S.GRAY}{hint}{S.R}\n")


def _run_user_command(command: str) -> str:
    """Run what the user typed after `!`, with the approval gate lifted.

    The prompt and the permission rules exist to put a person between the model
    and the machine. Here the person *is* the one asking, so making them approve
    their own keystrokes would be theatre. Everything else that makes `run_cmd`
    survivable is exactly what is wanted - the idle detection, the timeout, the
    output trimming, the session left registered when a program stops at a
    prompt - so this is `safe_run_cmd` with only the gate held open, the same
    way `dispatch_tool` holds it open for an allow rule.
    """
    config.POLICY_AUTO_ALLOW = True
    try:
        return tools.safe_run_cmd(command)
    finally:
        config.POLICY_AUTO_ALLOW = False


def _attach_mentions(text: str) -> str:
    """Expand `@path` in a typed message and say what came with it.

    The model is never told a mention failed by silence: a path that is not
    there is named on screen, and the message goes as written.
    """
    expanded, notes = mentions.expand(text)
    for path, attached, detail in notes:
        if attached:
            print(f"  {S.MUTED}◆ attached {S.WHITE}{path}{S.R} {S.GRAY}({detail}){S.R}")
        else:
            print(f"  {S.WARN}⚠ @{path}: {detail}{S.R}")
    if notes:
        print()
    return expanded


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


def _report_strays() -> None:
    """Point out state an older version wrote into this directory.

    Named, never touched. `sessions` and `memory.json` are ordinary enough
    names that moving one on sight would eventually take somebody's real work
    with it - so this says what it found and what to type, and stops there.
    """
    strays = paths.strays_in_cwd()
    if not strays:
        return
    print(f"  {S.MUTED}\u25c6 {', '.join(strays)} here look like state from an "
          f"older version.{S.R}")
    print(f"  {S.MUTED}  It now lives in {paths.home()}. Nothing has been moved; "
          f"to move it:{S.R}")
    print(f"  {S.GRAY}    mv {' '.join(strays)} {paths.home()}/{S.R}\n")


def _agent_label() -> str:
    """How this session appears to the other agents: its model, and its subject."""
    if config.SESSION_TITLE:
        return f"{config.MODEL} - {config.SESSION_TITLE}"
    return config.MODEL


def _report_agents(agent_id: str) -> None:
    """Say who else is already working here. Silent when nobody is.

    A solo session should not be told about a feature it has no use for, and a
    second terminal opened on the same project should not have to go looking to
    find out it is not alone.
    """
    if not agent_id:
        return
    others = channel.peers()
    if not others:
        return
    print(f"  {S.INFO}◆ You are agent {S.BOLD}{agent_id}{S.R}{S.INFO} here. "
          f"{len(others)} other agent(s) working in this project:{S.R}")
    for record in others:
        holds = ", ".join(record.get("holds") or [])
        print(f"  {S.GRAY}•{S.R} {S.WHITE}{record['id']}{S.R} "
              f"{S.MUTED}{record.get('label') or 'unknown model'}"
              f"{'  holding ' + holds if holds else ''}{S.R}")
    print(f"  {S.MUTED}  They can see you too. {S.GRAY}/agents{S.MUTED} for the "
          f"board.{S.R}\n")


def _show_arrivals() -> None:
    """Print what other agents have said, as soon as the terminal is free.

    The model gets the same messages at the start of its next turn - the two
    have separate cursors on purpose, because a message the person has read on
    screen has not yet been read by the model, and the other way round.
    """
    try:
        for entry in channel.take_for_screen():
            print(f"  {S.PURPLE}✉ {channel.describe(entry)}{S.R}")
    except Exception:
        pass          # the board is a convenience; it never stops the prompt


async def _watch_channel() -> None:
    """While a prompt is waiting for a line, watch for another agent's message.

    Without this a reply only appears after the person presses Enter, which for
    the agent that asked the question means the answer arrives at some point
    after it stopped being useful. It runs only for as long as the prompt is
    open, so nothing here can print over a streaming answer.
    """
    while True:
        await asyncio.sleep(max(0.5, float(getattr(config, "CHANNEL_POLL_SECONDS", 2))))
        try:
            channel.heartbeat(_agent_label())
            _show_arrivals()
        except Exception:
            return


async def _read_line(session_pt) -> str:
    """One line from the person, with the channel watched while they type."""
    if session_pt is None:
        _show_arrivals()
        return input(f"  {S.USER_CLR}{S.BOLD}❯{S.R} ").strip()

    _show_arrivals()
    # `ANSI` lives behind the prompt_toolkit guard in `config`, so it is reached
    # the same way `main` reaches it rather than imported at module level.
    ANSI = config.ANSI
    watcher = asyncio.ensure_future(_watch_channel())
    # `patch_stdout` is what lets the watcher print *above* the prompt rather
    # than through the middle of what is being typed.
    keep_prompt_intact = getattr(config, "patch_stdout", None)
    try:
        if keep_prompt_intact is None:
            return (await session_pt.prompt_async(
                ANSI(f"  {S.USER_CLR}{S.BOLD}❯{S.R} "))).strip()
        with keep_prompt_intact():
            return (await session_pt.prompt_async(
                ANSI(f"  {S.USER_CLR}{S.BOLD}❯{S.R} "))).strip()
    finally:
        watcher.cancel()


def _agents_command(rest: str) -> None:
    """`/agents`, and the three things a person can do to the board by hand."""
    # Split on the first word rather than matched with `startswith`: otherwise
    # `/agents saying hello` would be read as `say` and send "ing hello".
    verb, _, argument = rest.strip().partition(" ")
    verb, argument = verb.lower(), argument.strip()

    if verb in ("on", "off") and not argument:
        config.CHANNEL_ENABLED = verb == "on"
        if config.CHANNEL_ENABLED:
            agent_id = channel.join(_agent_label())
            print(f"  {S.OK}✓ Agent channel is ON.{S.MUTED} This session is "
                  f"{agent_id or 'unregistered'}.{S.R}\n")
        else:
            channel.leave()
            print(f"  {S.INFO}✓ Agent channel is OFF.{S.MUTED} Other agents can "
                  f"no longer see this session, and its claims are released.{S.R}\n")
        return

    if verb == "say":
        if not argument:
            print(f"  {S.ERR}✗ Usage: /agents say <message>{S.R}\n")
            return
        ok, said = channel.send(argument)
        print(f"  {S.OK}✓ {said.capitalize()}.{S.R}\n" if ok
              else f"  {S.ERR}✗ Not sent: {said}.{S.R}\n")
        return

    if verb == "release":
        target = argument
        if not target:
            print(f"  {S.ERR}✗ Usage: /agents release <path>{S.R}\n")
            return
        # The one way past another agent's claim, and deliberately not something
        # the model can reach: the person is the only one here who can see both
        # terminals and decide which of them should be holding the file.
        dropped = channel.force_release(target)
        print(f"  {S.OK}✓ Released {', '.join(dropped)}.{S.R}\n" if dropped
              else f"  {S.WARN}⚠ Nothing there is claimed: {target}{S.R}\n")
        return

    _show_agents()


def _set_command(rest: str, messages: list[dict]) -> None:
    """`/set`: read and change a setting without editing `config.py`.

    `/set`              every setting, with the changed ones marked
    `/set NAME`         one of them
    `/set NAME value`   change it, for this session and the next
    `/set NAME default` put it back to what `config.py` says
    """
    name, _, value = rest.strip().partition(" ")
    if not name or not value.strip():
        _show_settings(name)
        return

    ok, detail = config.set_setting(name, value)
    if not ok:
        print(f"  {S.ERR}✗ {detail}{S.R}\n")
        return

    name = name.strip().upper()
    print(f"  {S.OK}✓ {name} = {_fmt_setting(getattr(config, name))}{S.R} "
          f"{S.MUTED}({detail}){S.R}")
    # Some of them are part of the system prompt - NATIVE_TOOLS decides whether
    # the tool catalogue is in it at all - so it is rebuilt every time rather
    # than only for the ones somebody remembered to list here.
    _refresh_system_prompt(messages)
    print()


def _vm_command(rest: str) -> None:
    """`/vm`: what the Python scratch process is holding, and how to clear it.

    The VM keeps a namespace across a whole session and lives in a directory of
    its own, so both are things a person may reasonably want to see or empty
    without having to ask the model to do it for them.
    """
    verb = rest.strip().lower()
    state = vm.state()

    if verb in ("reset", "clear"):
        result = vm.run("pass", reset=True)
        print(f"  {S.OK}✓ The Python VM is empty again.{S.R}\n" if not result.get("crashed")
              else f"  {S.ERR}✗ {result['crashed']}.{S.R}\n")
        return

    if verb in ("stop", "off"):
        vm.shutdown()
        print(f"  {S.INFO}✓ The Python VM was stopped.{S.MUTED} The next "
              f"run_python starts a new one.{S.R}\n")
        return

    print()
    print(f"  {S.BOLD}{S.ACCENT}Python VM{S.R}")
    print(f"  {_hr(width=44)}")
    if not state["alive"]:
        print(f"  {S.MUTED}not running{S.R} {S.GRAY}- the next run_python starts it{S.R}")
    else:
        print(f"  {S.OK}running{S.R}  {S.GRAY}{state['calls']} call(s), "
              f"up {state['age']:.0f}s{S.R}")
    print(f"  {S.GRAY}scratch directory:{S.R} {state['directory']}")
    print(f"  {S.GRAY}per call:{S.R} {config.VM_TIMEOUT}s, "
          f"{config.VM_OUTPUT_CHARS} chars of output"
          + (f", {config.VM_MEMORY_MB}MB" if os.name != "nt" else ""))
    print(f"  {S.MUTED}/vm reset{S.R} {S.GRAY}empties it, {S.MUTED}/vm stop{S.GRAY} "
          f"ends the process{S.R}\n")


def _show_agents() -> None:
    """The board: who is here, what they are holding, and what has been said."""
    if not config.CHANNEL_ENABLED:
        print(f"  {S.WARN}⚠ The agent channel is off.{S.MUTED} "
              f"{S.GRAY}/agents on{S.MUTED} turns it back on.{S.R}\n")
        return
    here = channel.agents()
    mine = channel.me()
    print()
    print(f"  {S.BOLD}{S.ACCENT}Agents in {channel.workspace()}{S.R}")
    print(f"  {_hr(width=44)}")
    if not here:
        print(f"  {S.GRAY}nobody, not even this session{S.R}")
    for record in here:
        who = f"{record['id']} (you)" if record["id"] == mine else record["id"]
        holds = ", ".join(record.get("holds") or []) or "nothing"
        print(f"  {S.ACCENT}{who:12}{S.R} {S.WHITE}{record.get('label') or '?'}{S.R}")
        print(f"  {' ' * 12} {S.MUTED}started {channel.ago(record.get('started'))}, "
              f"holding {holds}{S.R}")
    recent = channel.read_board()["messages"][-6:]
    if recent:
        print()
        print(f"  {S.BOLD}{S.ACCENT}Recently said{S.R}")
        print(f"  {_hr(width=44)}")
        for entry in recent:
            print(f"  {S.MUTED}{channel.ago(entry.get('at')):>8}{S.R} "
                  f"{S.GRAY}{channel.describe(entry)}{S.R}")
    print(f"\n  {S.MUTED}/agents say <text> to talk to them, /agents release "
          f"<path> to take a file back.{S.R}\n")


async def main(resume_id: str = "") -> None:

    if config.CURRENT_OS == "Windows":
        os.system("")

    print("\033[2J\033[H", end="")
    providers.apply_startup()
    failed_mcp = _connect_mcp_servers()
    config.SYSTEM_PROMPT = _build_system_prompt()
    messages: list[dict] = [{"role": "system", "content": _compose_system_prompt()}]

    current_session_id = None
    # Adopted before the banner, so the banner reports the resumed session's
    # model rather than the one it is about to be replaced by; replayed after
    # it, so the transcript reads downwards from the header as it did the first
    # time. `cli()` has already established that the id names a real file.
    resumed = load_session(resume_id) if resume_id else None
    if resumed:
        messages = _adopt_session(resumed)
        current_session_id = resume_id

    _welcome()
    _report_mcp_problems(failed_mcp)
    _report_strays()
    _report_agents(channel.join(_agent_label()))

    if resume_id:
        if resumed:
            print(f"  {S.OK}✓ Resumed session: {config.SESSION_TITLE or resume_id} "
                  f"(Model: {config.MODEL}){S.R}\n")
            _replay_session(messages)
        else:
            print(f"  {S.ERR}✗ Session not found: {resume_id}{S.R}")
            print(f"  {S.GRAY}Starting a new one instead.{S.R}\n")

    if config.PROMPT_TOOLKIT_AVAILABLE:
        paths.ensure_home()          # FileHistory opens its file straight away
        from simple_harness.config import (SlashCommandCompleter, PathMentionCompleter,
                                      merge_completers, PromptSession, FileHistory)
        completer = merge_completers([
            SlashCommandCompleter(['/help', '/clear', '/usage', '/model', '/models', '/exit', '/quit', '/sessions', '/load', '/title', '/autotitle', '/automode', '/fullcontent', '/record', '/export', '/system', '/planmode', '/skills', '/skill', '/mcp', '/perms', '/think', '/connect', '/undo', '/autocommit', '/deepthink', '/agents', '/vm', '/set']),
            PathMentionCompleter(),
        ])
        session_pt = PromptSession(
            history=FileHistory(config.HISTORY_FILE),
            completer=completer,
            # The menu has to open on its own for `@` to be discoverable: nobody
            # presses Tab after a character they have not been told completes.
            complete_while_typing=True,
        )

    while True:
        try:
            user_input = await _read_line(
                session_pt if config.PROMPT_TOOLKIT_AVAILABLE else None)
            # A console that hands back surrogate escapes would otherwise poison
            # the history: every later save and request would raise.
            user_input = config.safe_text(user_input)
        except (EOFError, KeyboardInterrupt):
            channel.leave()
            mcp_client.shutdown()
            print(f"\n\n  {S.GRAY}Goodbye!{S.R}\n")
            break

        if not user_input:
            continue

        if user_input.startswith("!"):
            # The user's own command, not the model's. It still goes into the
            # conversation, because the reason to run `!git status` mid-chat is
            # almost always so that the next question can be about its output.
            command = user_input[1:].strip()
            if not command:
                print(f"  {S.ERR}✗ Usage: !<shell command>{S.R}\n")
                continue
            print(f"\n  {S.MUTED}${S.R} {S.WHITE}{command}{S.R}")
            output = _run_user_command(command)
            _fmt_tool_result(command, output)
            print()
            messages.append({"role": "user",
                             "content": f"[Shell] $ {command}\n{output}"})
            current_session_id = save_session(messages, current_session_id)
            continue

        cmd = user_input.lower()
        if cmd in ("/exit", "/quit"):
            channel.leave()
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
            config.turn_index = 0
            config.LOADED_SKILLS.clear()
            print("\033[2J\033[H", end="")
            _welcome()
            print(f"  {S.OK}✓ Conversation and usage cleared.{S.R}\n")
            continue
        if cmd == "/models":
            provider = providers.current()
            print(f"\n  {S.BOLD}{S.ACCENT}{provider.label} Models{S.R}")
            print(f"  {_hr(width=50)}")
            try:
                available = provider.list_models()
                if not available:
                    print(f"  {S.WARN}\u26a0 None found.{S.R}")
                for i, entry in enumerate(available, 1):
                    marker = f" {S.OK}\u25c0 current{S.R}" if entry["name"] == config.MODEL else ""
                    detail = f"  {S.GRAY}({entry['detail']}){S.R}" if entry.get("detail") else ""
                    print(f"  {S.ACCENT}{i:3}.{S.R} {S.WHITE}{entry['name']}{S.R}{detail}{marker}")
            except Exception as e:
                print(f"  {S.ERR}\u2717 Failed to list models: {e}{S.R}")
            print()
            continue
        if cmd == "/model":
            provider = providers.current()
            print(f"\n  {S.GRAY}provider{S.R}  {S.WHITE}{provider.label}{S.R}")
            print(f"  {S.GRAY}model{S.R}     {S.WHITE}{config.MODEL}{S.R}\n")
            connect.run(provider.name)
            continue
        if cmd == "/connect" or cmd.startswith("/connect "):
            connect.run(user_input.split(" ", 1)[1].strip() if " " in user_input else "")
            _refresh_system_prompt(messages)
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
                _show_ambiguous(query, matches, "Re-run /load with one of the ids above.")
                continue
            sid = matches[0][0] if matches else query
            loaded = load_session(sid)
            if loaded:
                messages = _adopt_session(loaded)
                current_session_id = sid
                label = config.SESSION_TITLE or sid
                print(f"  {S.OK}✓ Loaded session: {label} (Model: {config.MODEL}){S.R}\n")
                _replay_session(messages)
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

        if cmd == "/undo":
            ok, message = git_ops.undo_last()
            colour = S.OK if ok else S.WARN
            print(f"  {colour}{'✓' if ok else '⚠'} {message}{S.R}\n")
            continue

        if cmd == "/autocommit" or cmd.startswith("/autocommit "):
            parts = cmd.split(" ", 1)
            setting = parts[1].strip() if len(parts) > 1 else ""
            if setting not in ("on", "off"):
                state = "ON" if config.GIT_AUTO_COMMIT else "OFF"
                print(f"  {S.INFO}Auto-commit is {S.BOLD}{state}{S.R}"
                      f"{S.MUTED} - each file an AI tool changes is committed on its own.{S.R}")
                if not git_ops.repo_root():
                    print(f"  {S.MUTED}This directory is not a git repository, so nothing "
                          f"is committed either way.{S.R}")
                recent = git_ops.recent_ai_commits(5)
                for commit in recent:
                    print(f"  {S.MUTED}│{S.R} {S.GRAY}{commit['sha']}{S.R} "
                          f"{commit['subject']} {S.MUTED}({commit['when']}){S.R}")
                if recent:
                    print(f"  {S.MUTED}╰─ /undo takes the newest one back{S.R}")
                print(f"  {S.MUTED}Usage: /autocommit <on/off>{S.R}\n")
                continue
            config.GIT_AUTO_COMMIT = setting == "on"
            print(f"  {S.INFO}✓ Auto-commit is "
                  f"{'ON' if config.GIT_AUTO_COMMIT else 'OFF'}."
                  f"{S.MUTED} {'Each AI edit gets its own commit; /undo takes one back.' if config.GIT_AUTO_COMMIT else 'AI edits are no longer committed for you.'}{S.R}\n")
            continue

        if cmd == "/deepthink" or cmd.startswith("/deepthink "):
            parts = cmd.split(" ", 1)
            setting = parts[1].strip() if len(parts) > 1 else ""
            if setting not in ("on", "off"):
                state = "ON" if config.DEEPTHINK else "OFF"
                print(f"  {S.INFO}Deepthink is {S.BOLD}{state}{S.R}"
                      f"{S.MUTED} - one request becomes "
                      f"{len(deepthink.STAGES)} turns:{S.R}")
                for i, stage in enumerate(deepthink.STAGES, 1):
                    print(f"  {S.MUTED}│{S.R} {S.GRAY}{i}.{S.R} {stage.title}")
                print(f"  {S.MUTED}╰─ a request that needs no changes stops "
                      f"after the first.{S.R}")
                print(f"  {S.MUTED}Usage: /deepthink <on/off>{S.R}\n")
                continue
            config.DEEPTHINK = setting == "on"
            if config.DEEPTHINK:
                print(f"  {S.INFO}✓ Deepthink is ON.{S.MUTED} Say what you want built "
                      f"and it will plan, argue with the plan, build it, review the "
                      f"diff, then run it.{S.R}\n")
            else:
                print(f"  {S.INFO}✓ Deepthink is OFF.{S.MUTED} Back to one turn per "
                      f"request.{S.R}\n")
            continue

        if cmd == "/agents" or cmd.startswith("/agents "):
            _agents_command(user_input[len("/agents"):])
            continue

        if cmd == "/vm" or cmd.startswith("/vm "):
            _vm_command(user_input[len("/vm"):])
            continue

        if cmd == "/set" or cmd.startswith("/set "):
            # The user's own casing, not `cmd`: a string setting keeps what
            # they typed, and only the name is case-insensitive.
            _set_command(user_input[len("/set"):], messages)
            continue

        # Every slash command has had its turn and continued; what is left is a
        # message for the model, so this is where an `@path` becomes context.
        # Before the plan-mode note, so the attachment stays under the sentence
        # the user wrote rather than under a system aside.
        user_input = _attach_mentions(user_input)

        if config.PLANMODE and not config.DEEPTHINK:
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

        # What the other agents said reaches the model here, ahead of the user's
        # own message so that the request stays the last thing in the history.
        # The person has already seen these at the prompt - the two cursors are
        # separate - so this only says that they were passed on.
        channel.heartbeat(_agent_label())
        arrived = channel.turn_note()
        if arrived:
            messages.append({"role": "user", "content": arrived})
            print(f"  {S.MUTED}↪ passed the channel messages to the model{S.R}")

        # One turn starts here and covers everything done to answer it - the
        # tool loop's follow-up requests, deepthink's six stages, any sub-agent -
        # so `/usage` reports what the question cost rather than what its last
        # request cost.
        config.next_turn()
        messages.append({"role": "user", "content": user_input})
        config.repair_messages(messages)
        current_session_id = save_session(messages, current_session_id)

        try:
            if config.DEEPTHINK:
                # deepthink drives its own turns, and manages context between
                # them - it is several passes over one request, not one.
                result = await deepthink.run(messages)
            else:
                await manage_context(messages)
                result = await chat_turn(messages)

            current_session_id = save_session(messages, current_session_id)

            # Name the session once, from the exchange that just finished.
            if config.AUTO_TITLE and not config.SESSION_TITLE and current_session_id:
                sys.stdout.write(f"  {S.MUTED}✎ naming session…{S.R}")
                sys.stdout.flush()
                title = await generate_session_title(messages)
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                if title:
                    current_session_id = rename_session(current_session_id, title)
                    print(f"  {S.MUTED}✎ session titled: {S.GRAY}{config.SESSION_TITLE}{S.R}\n")

        except Exception as e:
            print(f"\n  {S.ERR}✗ Error: {e}{S.R}\n")


def _use_utf8_output() -> None:
    """Make sure the harness can print its own interface.

    The TUI is drawn with box characters - the tool call alone uses U+25B8 and
    U+2570 - and an answer is routinely not ASCII either. A Windows console
    handles those, but a *pipe* on Windows does not: Python falls back to the
    locale code page there, cp1252 or cp949, and the first tool call raises
    UnicodeEncodeError halfway through drawing itself. Redirecting the output
    to a file should not crash the program.

    `errors="replace"` rather than "strict" for the same reason: a character
    the terminal genuinely cannot show is worth one replacement glyph, never a
    traceback in the middle of an answer.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass          # not a real stream, or an encoding it will not take


def _parse_args(argv: list) -> argparse.Namespace:
    """Answer `--help` and `--version`, and take the session to pick up from.

    Almost everything is a slash command once the harness is running; what
    cannot be is which conversation to open, because by the time there is a
    prompt to type at, a new one has already been started. So the two ways of
    naming one live here: `--resume` for a session you can name, and `-c` for
    the one you were last in, in this directory.

    Both spellings of each are accepted. `-resume` is the single dash the
    request was written with, and `--resume` is what anyone who has used
    another CLI will reach for first; refusing either would be pedantry.
    """
    parser = argparse.ArgumentParser(
        prog="simple-harness",
        description="A terminal AI assistant built for small local models.",
        epilog="Run with no arguments to start a new session. Everything else is a "
               "slash command inside it - type /help there for the list.")
    parser.add_argument("-V", "--version", action="version",
                        version=f"simple-harness {__version__}")
    picked = parser.add_mutually_exclusive_group()
    picked.add_argument("-r", "-resume", "--resume", metavar="ID", dest="resume",
                        help="resume a saved session by id or title (see /sessions)")
    picked.add_argument("-c", "-continue", "--continue", dest="continue_here",
                        action="store_true",
                        help="resume the newest session last worked on in this directory")
    return parser.parse_args(argv)


def _session_to_resume(args: argparse.Namespace) -> str:
    """The session id the command line asks for, or "" for a new conversation.

    Neither flag falls back to a fresh session when it cannot find one, and
    neither guesses between candidates: being dropped into an empty prompt when
    you asked to continue something is how an afternoon's context gets lost
    quietly. Both say what they looked for and stop.
    """
    if getattr(args, "continue_here", False):
        here = os.getcwd()
        sid = latest_in_dir(here)
        if not sid:
            print(f"\n  {S.ERR}✗ No saved session was last worked on here:{S.R} {S.WHITE}{here}{S.R}")
            print(f"  {S.GRAY}Run with no arguments to start one, or name another with "
                  f"{S.ACCENT}--resume <id>{S.GRAY}.{S.R}\n")
            raise SystemExit(1)
        return sid

    query = (args.resume or "").strip()
    if not query:
        return ""
    matches = find_sessions(query)
    if not matches:
        print(f"\n  {S.ERR}✗ No saved session matches: {query}{S.R}")
        print(f"  {S.GRAY}Sessions are listed by {S.ACCENT}/sessions{S.GRAY} inside one.{S.R}\n")
        raise SystemExit(1)
    if len(matches) > 1:
        print()
        _show_ambiguous(query, matches, "Re-run with one of the ids above.")
        raise SystemExit(1)
    return matches[0][0]


def cli() -> None:
    """The `simple-harness` command, and what `python -m simple_harness` runs.

    `main()` is a coroutine, and a console-script entry point has to be an
    ordinary function - so the event loop and the two exits that are not errors
    are handled here rather than under `__main__`, where an installed copy
    would never reach them.
    """
    _use_utf8_output()
    args = _parse_args(sys.argv[1:])
    # Before anything is started, and before the first turn can ask to run a
    # command: what this does to the machine it is on. Asked once per machine.
    if not terms.require():
        raise SystemExit(1)
    # Resolved here rather than inside `main()`: a name that matches nothing, or
    # matches several, is a mistake in the command that was typed, and the place
    # to answer it is before a screen has been cleared and servers started.
    resume_id = _session_to_resume(args)
    try:
        asyncio.run(main(resume_id))
    except KeyboardInterrupt:
        print(f"\n\n  {S.GRAY}Goodbye!{S.R}\n")
    except Exception as e:
        print(f"\n  {S.ERR}✗ Unexpected error: {e}{S.R}")


if __name__ == "__main__":
    cli()

# Simple Harness

[![CI](https://github.com/minjun1177/simple_harness/actions/workflows/ci.yml/badge.svg)](https://github.com/minjun1177/simple_harness/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

## 1. What It Does

A terminal AI assistant that can actually work on your machine: read and write
files, run commands and answer their prompts, search the web, and undo what it
did. It runs against a local Ollama model or a hosted one (Anthropic, OpenAI,
Gemini) with the same tools and the same safety prompts either way.

It is built for the local case first. A 4B model cannot reliably escape a
source file into a JSON string, so it is never asked to; much of what is here
exists to make small models genuinely usable rather than nearly usable.

---

## 2. Features

- **Any Provider**: `/connect` points the harness at Ollama, Anthropic, OpenAI (or anything OpenAI-compatible), or Google Gemini. No vendor SDKs - four wire formats normalised into one event shape.
- **Two Tool Protocols, One Tool Table**: A model with a real function-calling interface gets the tools through it; one without gets them as `<tool_call>` text with a JSON repair engine behind it. For Ollama this is decided per model. Both come from the same table, and both end up as the same call.
- **Deepthink**: `/deepthink on` turns one request into plan → argue with the plan → build → review the real diff → run it. The planning stages *cannot* edit, and the harness reports what the final check actually ran.
- **Undo**: every file an AI tool changes is committed on its own, so `/undo` takes it back. It commits only what the tool named, and refuses to undo over work it did not create.
- **Sub-agents**: `spawn_agent` hires a second model for one self-contained job. It works in its own context and hands back only its report, so a twenty-tool-call search never enters the conversation.
- **Crash-safe writes**: sessions, memory, permission rules and saved API keys are written to a temporary file and renamed into place, so being killed mid-write cannot empty one.
- **ANSI Terminal User Interface**: Provides an ANSI-colored TUI with streaming text responses, live token-per-second (TPS) calculation, custom spinner animations, markdown rendering, syntax code blocks, and ASCII tables.
- **Interactive Action Approval**: Security layer that prompts the user for confirmation prior to running shell commands, editing/writing files, or sending network API requests.
- **Dynamic Context Compression**: Monitors active token counts and conversation length to automatically condense conversation history when nearing model limits, tailored to model size.
- **Persistent Memory Storage**: Long-term key-value memory storage system backed by `memory.json` to store user preferences, facts, and instructions across sessions.
- **Session & History Management**: Save, list, load, record, and export conversation transcripts in JSON or Markdown format.
- **Named Sessions**: Sessions are filed under a readable title instead of a timestamp. The model names each new session after its first exchange (`/autotitle off` to stop it), `/title <name>` renames it by hand, and `/load` accepts either the title or the id.
- **Enhanced Terminal Shell**: Input autocompletion for slash commands and persistent input history across restarts powered by `prompt_toolkit`.
- **Hashline Line-Level Hashing**: File reading appends 2-character MD5 hashes to line numbers (`LINE_NUM:HASH|`) allowing the agent to target precise code locations when making edits.
- **Multi-Source Web Search**: Queries several keyless sources (DuckDuckGo, Wikipedia, Stack Exchange, GitHub, optional self-hosted SearXNG), reads the actual pages, ranks passages locally with BM25, and reports "no relevant results" rather than returning off-topic pages.
- **Agent Skills**: Folder-based instruction packs (`skills/<name>/SKILL.md`) that the model loads on demand. Only each skill's name and description sit in the system prompt, so a large library stays cheap until a skill is actually needed.
- **Tool Permissions**: Rules in `.permissions.json` decide what runs without asking and what never runs at all, filling the gap between prompting for everything and `/automode` allowing everything. Answering `a` at any approval prompt saves a rule.
- **Reasoning Model Support**: A model's `<think>` blocks (and Ollama's separate `thinking` field) are kept out of the answer, off the screen by default, and out of the conversation history - so scratch work never eats the context budget.
- **MCP Servers**: Any Model Context Protocol server declared in `.mcp.json` is started with the app, and its tools join the built-in ones as `mcp__<server>__<tool>`. Local subprocesses (stdio) and remote endpoints (streamable HTTP, legacy SSE) are all supported, with the same approval prompt guarding every call.

---

## 3. Setup

### Prerequisites
- **Python**: Version 3.10 or higher
- **Ollama**: Installed and running locally (default endpoint: `http://localhost:11434`).
  Only needed for the local case - `/connect` reaches Anthropic, OpenAI and
  Gemini without it.

### Installation Steps

1. **Install it**:
   ```bash
   git clone https://github.com/minjun1177/simple_harness
   cd chat
   pip install -e .
   ```
   Or, to run it straight from the checkout without installing:
   ```bash
   pip install -r requirements.txt
   ```
   `get_code_skeleton` and `query_ast_node` need Tree-sitter, which is ten
   grammar wheels for two tools and so is opt-in: `pip install -e ".[ast]"`.
   Everything else runs without it.

2. **Pull an Ollama Model**:
   ```bash
   ollama pull gemma4:e4b
   ```

3. **Launch it**:
   ```bash
   simple-harness            # if you installed it
   python -m simple_harness   # if you did not
   ```

### Running the tests

No framework - each file is a script that prints `[ok]` / `[FAIL]` and exits
non-zero on failure. They need no Ollama daemon and no network:

```bash
for t in tests/*.py; do python "$t" || echo "FAILED: $t"; done
```

`tests/test_platform.py` is the one worth running on any new machine, and
especially on Windows: it checks what that machine can tell you about a command
waiting for input.

---

## 4. Tool Capabilities

The client equips the model with 28 tools. They are listed in one table in
`toolspec.py`, from which both the system prompt and the dispatcher are
generated - so this list cannot quietly drift from what actually runs.

### Tool Call Format

A model with a real function-calling interface just calls the tool, and none of
this section applies to it - skip to *How tools are asked for* under Providers.

Everything below is the **text protocol**, used for models that have no such
interface. The model emits a `<tool_call>` block. Anything a parameter can hold
in one line goes in the JSON; a file body does not:

```
<tool_call>
{"name": "write_file", "arguments": {"filepath": "game.py"}}
<content>
import random

print("Guess the number!")
</content>
</tool_call>
```

Escaping a whole source file into a JSON string is the single thing small local
models get wrong most often - a bare quote inside `print("hi")`, a lost
backslash before a line continuation, one uncounted brace - and any of them used
to throw the entire generation away. A raw block removes the requirement: the
text is written exactly as it belongs on disk, with no escaping at all. `<content>`
feeds `write_file`; `<old_content>` and `<new_content>` feed `edit_file`.

Plain JSON still works. When it arrives damaged, the parser repairs what is
unambiguously safe - unclosed brackets, parameters the model put beside `name`
instead of inside `arguments`, a payload whose quotes broke the JSON around it -
and says so. What it refuses to repair is a reply that stopped early: closing
the brackets there would invent arguments that were never sent, and `write_file`
would happily write the empty result over a real file. Those are reported, and
the model is asked to send the call again.

**The envelope is repaired too.** A model with no tool-calling template of its
own does not reach for `<tool_call>`; it reaches for the nearest thing it knows.
Gemma writes a markdown fence, and it writes the name under `tool_name`, and it
nests the whole call under a `tool_call` key:

````
```tool_call
{"tool_name": "write_file", "arguments": {"filepath": "hello.py"}}
```
<content>
print('hi')
</content>
````

The JSON inside is usually byte-perfect and the raw block is byte-perfect - only
the wrapper is wrong. Reading only the literal tag found nothing there, so the
turn ended with no tool run, no error and nothing said, which is the one failure
this protocol exists to prevent. All three shapes are now read.

The refusals are what keep that honest. A fence is only read as a call when
there is no `<tool_call>` anywhere in the reply, so a correctly formatted call is
never second-guessed; and unless the fence says `tool_call` or `tool_code`
outright, what it holds has to decode to a tool that actually exists. An
ordinary ```json block in an answer stays an answer.

### Web & Network Tools
- `search_web`: Multi-source search with local relevance ranking (see Web Search below).
- `get_url`: Fetch web page contents and strip raw HTML down to readable text.
- `call_api`: Execute HTTP requests (GET, POST, PUT, PATCH, DELETE) with custom headers and JSON/text payloads.

### File System & Workspace Tools
- `read_file`: Read contents of a local file formatted with line numbers and line MD5 hashes.
- `write_file`: Create new files or overwrite existing file content. The body comes in a `<content>` raw block.
- `edit_file`: Find and replace specific content snippets within an existing file, via `<old_content>` / `<new_content>` raw blocks.
- `delete_file`: Remove a file from disk.
- `copy_file`: Copy a file to a new location.
- `create_dir`: Create a new directory path.
- `list_dir`: Display directory contents.
- `search_in_file`: Search workspace files for string or regex patterns (grep functionality).

### System & Git Management
- `run_cmd`: Execute system shell commands (requires approval). Stays connected to the command and reports when it is waiting for input.
- `send_input`: Answer a running command's prompt and read what it prints next.
- `end_process`: Stop a command left running by `run_cmd`.
- `get_system_info`: Retrieve system CPU, memory usage, disk statistics, and top memory-consuming processes.
- `git_status`: Check current git repository status.
- `git_diff`: View current git working directory modifications.

### Memory & Interaction Tools
- `write_memory`: Save key information to persistent JSON storage.
- `read_memory`: Retrieve content of a specific stored memory item.
- `get_memory_list`: List stored memory IDs with timestamp and preview.
- `edit_memory`: Update content of an existing memory record.
- `delete_memory`: Remove a memory entry from disk.
- `get_user_input`: Ask the user one or more questions, each with its own list of options plus a free-text choice.

### MCP Tools
Present only when an MCP server is attached (see MCP Servers below).
- `mcp__<server>__<tool>`: Every tool each connected server exposes, listed in the system prompt with its own parameters.
- `list_mcp_resources`: List the resources the connected servers expose, with the URI needed to read each one.
- `read_mcp_resource`: Read one resource by URI.

### Workflow Tools
- `use_skill`: Load the full instructions of a skill listed in the system prompt.
- `submit_plan_for_approval`: Present a task plan and diff blueprint for approval before executing (plan mode).
- `get_code_skeleton`: Return a JSON outline of a source file's structure via Tree-sitter.
- `query_ast_node`: Search a source file for Tree-sitter S-expression patterns.
- `spawn_agent`: Hire a second model for one self-contained job. See below.

### Sub-agents

`spawn_agent` starts a fresh conversation - its own system prompt, its own
history, its own tool loop - gives it a written brief, and returns its final
report as the tool result. The user never sees the sub-agent's working; the
assistant never sees it either, only the report.

It is for work whose *output* matters and whose *process* does not: finding
where something is handled across a codebase, reading six files to answer one
question, checking a list of URLs. Twenty tool results that will never be needed
again fill the sub-agent's context instead of the conversation's.

```
spawn_agent(task="Find every place a session file is written, and report the
                  file and line of each.",
            context="I already know session.py:save_session is one of them.",
            model="qwen3:8b")        # optional - a cheaper model for a long search
```

What it may do:

- every tool the assistant has, except `get_user_input`, `submit_plan_for_approval`
  and `spawn_agent` itself. It has nobody to ask, no plan to submit, and hiring
  chains have unbounded cost;
- nothing the assistant could not have done. Its tool calls go through the same
  permission rules and raise the same approval prompts. A sub-agent is not a way
  around a `deny` rule;
- at most `SUBAGENT_MAX_TURNS` turns (12 by default), after which it is asked for
  a report from what it has rather than being cut off mid-search.

Starting one asks for approval, like running a command does - it costs a stretch
of time, and on a hosted model real money, before it reaches its first tool.
Allow it permanently with an `allow` rule for `spawn_agent`.

---

## 5. Providers

The harness starts on Ollama and stays there until told otherwise. `/connect`
moves it:

```
/connect                     pick a provider, then a model from its own list
/connect anthropic           pick a model from Anthropic
/connect openai gpt-4o       connect straight to a model
/connect status              every provider, and what each one still needs
```

| Provider | Endpoint | Key from |
| :--- | :--- | :--- |
| `ollama` | local, `OLLAMA_HOST` or a `base_url` | none needed |
| `anthropic` | `api.anthropic.com` | `ANTHROPIC_API_KEY` |
| `openai` | `api.openai.com/v1`, or any compatible `base_url` | `OPENAI_API_KEY` |
| `gemini` | `generativelanguage.googleapis.com` | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |

Because `base_url` is settable, the `openai` entry also reaches anything that
speaks the same protocol - a local vLLM or llama.cpp server, OpenRouter, Groq,
Together.

Keys are read from the environment first. A key typed at the `/connect` prompt
is written to `~/.localchat/providers.json` - never into the project directory,
which is a place people commit from. On Linux and macOS the file is owner-only
(0600) from the moment it is created. Windows has no POSIX mode bits, so there
the file takes whatever ACL its directory gives it; `%USERPROFILE%` is
per-user, but if that matters to you, keep the key in the environment instead.

### How tools are asked for

Two protocols, chosen by who is answering:

| | Tools are | Tool calls come back as |
| :--- | :--- | :--- |
| **Anthropic, OpenAI, Gemini** | sent with the request | the API's own tool-call events |
| **Ollama, model supports tools** | sent with the request | Ollama's own tool-call events |
| **Ollama, model does not** | listed in the system prompt | `<tool_call>` text, repaired if malformed |

For Ollama this is decided **per model, not per provider**: whether a model can
call a tool depends on the template it was built with, and Ollama says so
outright. Of the twenty models installed on the machine this was written on,
five have no tool support. Each one is asked once and the answer cached; a
daemon that is down, or a model that cannot be asked, means the text protocol -
which works everywhere.

That fallback is the point. A model whose template cannot format a tool call
will simply never make one, and the text protocol plus its JSON repair engine
is what makes those models usable at all. A model that *can* is more accurate
through the real interface, and about **12KB of prompt cheaper per turn**,
because the tool list no longer has to be spelled out:

```
text protocol      system prompt 17.8KB   (tool schemas + <tool_call> rules)
native tool calls   system prompt 5.1KB   (schemas travel with the request)
```

Both protocols describe the same tools, from the same table in `toolspec.py`,
so they cannot drift apart. Both end up as the same `(name, arguments)` pair,
so dispatch, permissions, display, session files and context compression see no
difference - the history stays plain text either way, and a session saved from
one provider replays under another.

Set `NATIVE_TOOLS = False` in `config.py` to force the text protocol everywhere,
which is what an OpenAI-compatible server without tool support needs.
`/connect status` shows which protocol is in use.

**Why this is still small.** `providers.py` normalises four wire formats into
one event shape - text, thinking, tool call, done - and everything downstream is
untouched by which provider is answering. No vendor SDKs.

The shapes that do differ are handled in one place: Anthropic and Gemini take
the system prompt as its own field rather than a message, Gemini calls the
assistant role `model`, and both want consecutive same-role messages merged -
which the harness produces constantly, since every tool result is its own user
message.

---

## 6. Web Search

A single general search engine answers the *entity* in a query and drops the
term that matters. Asked for `ollama num_ctx meaning` it returns ollama.com, the
Windows download page, and install blogs - none of which contain `num_ctx`. The
old implementation passed those straight to the model, which then answered
confidently and wrongly.

`websearch.py` fixes that in three stages:

1. **Candidates from complementary sources**, run concurrently. A general web
   index is weak on code identifiers, so Stack Exchange and GitHub are queried
   for those and Wikipedia for concepts. Keyword APIs receive a distilled query
   (`ollama num_ctx meaning` becomes `ollama num_ctx`); web engines get the
   original. If a source fails or times out, the rest still return.
2. **The pages are read**, not just their snippets, and split into passages
   ranked by BM25 whose IDF comes from the candidate pool itself - so a term in
   every candidate scores near zero and a rare one dominates. No model, no
   corpus, no network.
3. **A relevance floor.** The query's discriminative terms - explicit
   identifiers, or the rarest term the pool exposes - must actually appear in a
   passage. If nothing clears it, the tool reports that the search found nothing
   and names the pages it rejected, instead of handing over the closest junk.

Everything is free and keyless. To make candidate generation fully local too,
run a [SearXNG](https://github.com/searxng/searxng) instance and point
`config.SEARXNG_URL` at it (e.g. `"http://localhost:8080"`); it is then used as
the primary source and the public ones stay as backup.

Tuning knobs live in `config.py`: `SEARCH_CANDIDATES`, `SEARCH_FETCH_PAGES`,
`SEARCH_PASSAGE_CHARS`, `SEARCH_RESULT_CHARS`, and the three timeouts.

---

## 7. Skills

A skill is an instruction pack stored on disk that the model pulls in only when
it is relevant. This keeps the system prompt small no matter how many skills
exist: only `name` and `description` are always loaded, and the body arrives
when the model calls `use_skill`.

```
skills/
  git-commit/
    SKILL.md          <- required
    types.md          <- optional bundled files, listed with absolute paths
  quick-skill.md      <- one-file skill
```

Skills are searched in `./skills/` first, then `~/.localchat/skills/`; the first
match on a name wins, so a project skill overrides a personal one.

`SKILL.md` opens with YAML frontmatter:

```markdown
---
name: git-commit
description: Use when the user asks for a commit message. Triggers on "commit", "커밋".
allowed-tools: git_status, git_diff, run_cmd
---

Instructions in plain markdown.
```

- `name` — optional; the folder or file name is used when it is missing.
- `description` — the only thing the model sees before loading, so write it as
  *when to use this* and include the words a user would actually type.
- `allowed-tools` — optional; shown to the model as the tool set the skill
  expects. Advisory, not enforced by the harness.

Two skills ship with the repo: `git-commit` and `code-review`. See
`skills/README.md` for the full format reference.

They are *in the repository*, not in the installed package - skills are looked
for in the working directory and in `~/.localchat/skills/`, never next to the
code, so that a project's own skills win and an install cannot quietly add
instructions you did not write. From a `pip install`, copy the two you want:

```bash
git clone https://github.com/minjun1177/simple_harness
cp -r simple_harness/skills/* ~/.localchat/skills/
```

---

## 8. MCP Servers

[MCP](https://modelcontextprotocol.io) is the standard way to hand an assistant
tools it did not ship with - a filesystem browser, a database, an issue tracker.
Declare a server once and its tools appear alongside the built-in ones.

### Declaring a server

Servers are read from `./.mcp.json` first, then `~/.localchat/mcp.json`; a
project entry wins over a personal one with the same name. Copy
`.mcp.json.example` to get started.

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/work"]
    },
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"}
    }
  }
}
```

| Key | Meaning |
| :--- | :--- |
| `command`, `args`, `env`, `cwd` | Start a local server over **stdio**. |
| `url`, `headers` | Talk to a remote server over **streamable HTTP**. |
| `type` | `stdio`, `http`, or `sse` for the deprecated HTTP+SSE transport. Inferred from `command`/`url` when omitted. |
| `disabled` | `true` keeps the entry but does not start it. |
| `timeout` | Seconds to wait for a tool call from this server. |
| `autoApprove` | Tool names on this server that may run without an approval prompt. |
| `trust` | `true` auto-approves every tool on this server. |

`${VAR}` and `${env:VAR}` anywhere in a server entry are replaced with the
matching environment variable, so tokens stay out of the file.

### How it behaves

Every enabled server is started when the app launches, in parallel. Its tools
are fetched and written into the system prompt as `mcp__<server>__<tool>`, with
each tool's JSON Schema flattened into the same parameter list the built-in
tools use. A server that fails to start is reported and skipped - the app runs
without it. Server-declared `instructions` are passed through to the model, and
`readOnlyHint` / `destructiveHint` annotations are surfaced in the tool
description.

Calls go through the same approval prompt as `run_cmd` and file edits, so
nothing runs on an attached server without a `y` - unless `/automode on`,
`autoApprove`, or `trust` says otherwise.

Tuning knobs live in `config.py`: `MCP_ENABLED`, `MCP_STARTUP_TIMEOUT`,
`MCP_CALL_TIMEOUT`, `MCP_HTTP_TIMEOUT`, `MCP_RESULT_CHARS`,
`MCP_MAX_TOOLS_PER_SERVER`, `MCP_TRUSTED_SERVERS`, and
`MCP_AUTO_APPROVE_READONLY`.

### Inspecting and controlling

`/mcp` shows every configured server, its transport, what it exposes, and the
error behind any failure. `/mcp tools` expands the tool list, `/mcp resources`
lists readable resources, `/mcp reload` re-reads the config files and
reconnects, and `/mcp prompt <server> <name> key=value` runs a prompt template
the server offers as your next message.

The protocol client is a self-contained JSON-RPC implementation in
`mcp_client.py` - no SDK dependency, and no new packages to install.

---

## 9. Running Commands

`run_cmd` captures the command's output, so nothing the command prints reaches
the user's screen while it runs - including a prompt. An interactive program
therefore used to hang the whole app: it sat waiting on stdin with its question
captured and invisible, and there was no way to tell what it wanted or to answer
it.

The fix is not to deny it stdin. **The model answers the prompt.** The command
keeps a live pipe, and when it goes quiet its output so far is handed over with
a session id - see below. A command that never finishes is stopped at
`config.CMD_TIMEOUT` (120s) along with everything it started, and whatever it
printed first is kept.

### Answering a program while it runs

`run_cmd` does not wait for the command to finish. It stays connected, draining
output as it appears, and when the command is waiting the output so far comes
back with a session id:

```
추측:

[Waiting] 'python3 game.py' is still running and has printed nothing for 0.6s,
so it is most likely waiting for input. Answer it with send_input:
    {"name": "send_input", "arguments": {"session": "s1"}}
```

The model reads the prompt, answers it with `send_input`, and gets whatever the
program prints next - one exchange per turn, until the program exits. That is
how it tests something it has just written: it plays through the program
itself. `end_process` stops one that will not exit. The first few answers can
also be sent up front in a `<stdin>` block on `run_cmd`.

**Telling "waiting" from "busy".** Going quiet proves nothing on its own - a
program that is merely computing looks identical from outside. The best signal
each platform offers is used, strongest first:

| Signal | Where | What it proves |
| :--- | :--- | :--- |
| The output ends without a newline (`추측: `) | everywhere | It is shaped like a prompt |
| `/proc/<pid>/syscall` says a thread is parked in `read()` on fd 0 | Linux | It really is waiting on stdin - and a "no" is trusted too |
| The process tree has burned no CPU | Windows, macOS | It is idle, but sleeping and waiting look the same |
| Silence lasting `CMD_WAIT_TIMEOUT` | everywhere | Nothing better was available |

On Linux the `/proc` answer is exact in both directions, so a `sleep 3` is
simply waited out. Windows and macOS have no equivalent, so idle CPU only
shortens the wait to `CMD_IDLE_GRACE` instead of ending it - a long silent
pause may be offered to the model as a prompt, and an empty `send_input` picks
the output back up when it turns out not to be one.

To see what a given machine can actually manage:

```bash
python tests/test_platform.py
```

It prints which signals are available there, then runs a prompt, a silent
sleep, a busy loop and a runaway command through `run_cmd`. On Linux it does
the whole thing twice, the second time with `/proc` switched off - which is
exactly the code Windows and macOS run, so the fallback can be checked without
leaving Linux. It exits non-zero if anything fails.

A command that never stops printing is killed at `CMD_TIMEOUT` along with
everything it started, and at most `CMD_MAX_SESSIONS` live commands are kept.

**Text that is not ASCII.** A command's output is decoded as UTF-8 first,
because UTF-8 is the only candidate that can report that it is wrong - almost
any byte is legal cp949, so guessing a code page first would silently turn good
text into mojibake. If the bytes turn out not to be UTF-8, which is what a
Python older than 3.15 printing to a pipe on Korean Windows produces, the
console code page takes over for the rest of that command - both for what it
prints and for what `send_input` sends back to it.

---

## 10. Deepthink

Off by default. `/deepthink on` turns one request into six turns:

```
1  Plan      work out what it takes - read the files, change nothing
2  Check     argue against that plan and settle every assumption
3  Implement carry out the plan as it now stands
4  Review    read the diff of what actually changed, and list what is wrong
5  Revise    fix what the review found, and nothing else
6  Verify    run it, check it against the plan, report what really came back
```

All six are one conversation, so each stage sees everything the ones before it
did. What changes is the instruction at the top of each turn.

Asked to implement something, a model goes straight at it. It writes code from
what it remembers of a file rather than what the file says, and when it is done
it reports success without running the thing. Both come from the same place -
one pass, with no step whose only job is to find fault.

**Finding and fixing are two stages, not one.** Review used to do both, and a
stage that is allowed to fix stops looking as soon as it has something to fix -
so the rest of its own list went unread. Review is now read-only and its whole
output is a numbered list of what is wrong; stage 5 turns the tools back on and
works through that list, and is told not to widen it, because a change nobody
reviewed is a change nobody checked. An empty list means stage 5 changes
nothing, which is a result rather than an idle turn to fill.

**Three of the six carry the mode.** Stage 2 is the only one asked to prove the
plan wrong, and a plan nobody argued with is usually the one that fails. Stage 4
is handed the **real `git diff`** rather than being asked what it changed:
reviewing from memory finds nothing, because the memory is of the intention, not
of the code. Without git - no repository, or `/autocommit off` - it is told to
re-read the files instead. Stage 6 goes back to the plan and checks it item by
item, because code that runs and is not what was agreed is still not finished.

**The stages that are meant to think cannot edit.** Not "are asked not to" - the
tools that change things are switched off in stages 1, 2 and 4, and a model that
tries one is told to say what it would change instead. Telling a model to hold
off does not hold it off; a local 4B model tried to edit fifteen times in the
planning stage before this was enforced.

**It stops early when there is nothing to build.** A question costs one turn,
not six: the plan stage marks it, and if the model forgets to, the plan itself
is read back in one short call to decide. Anything unclear counts as work to do.
A build stage that changed nothing also ends the chain rather than reviewing and
verifying work that was never done.

```
/deepthink            the stages, and whether it is on
/deepthink on|off     turn it on or off
```

Deepthink supersedes plan mode while it is on, so `/planmode` injects nothing -
two sets of planning instructions only contradict each other.

---

## 11. Undoing AI Edits

Every file an AI tool changes is committed on its own, under a message naming
the tool that did it:

```
ai(edit_file): greet.py
ai(write_file): parser.py
```

`/undo` takes the newest one back:

```
/undo             put the last AI edit back the way it was
/autocommit       whether this is on, and the recent AI commits
/autocommit off   stop committing (edits still happen, they are just not committed)
```

An assistant that edits files is only as useful as its undo. Without one the
honest advice is "commit before you let it touch anything", which nobody
follows, and a wrong edit three tool calls ago is gone.

Two rules keep this from being a nuisance:

**It commits only what the tool named.** Whatever else you have staged or
changed is left exactly as it was - `git commit` is given those paths
explicitly rather than being allowed to sweep up your index.

**Undo refuses rather than destroying work it did not create.** It will not
touch a commit you wrote, and if a file in the AI's commit has changed since,
it stops and says which one:

```
'ai(write_file): shared.py' touched files that have since changed: shared.py.
Commit or discard those first - undoing now would take them with it.
```

Outside a git repository, and on a machine with no git installed, nothing is
committed and nothing breaks. Set `GIT_AUTO_COMMIT = False` in `config.py` to
default it off.

---

## 12. Tool Permissions

Until now the only gate was the approval prompt, and `/automode on` turned it
off for everything at once - including `run_cmd` and `delete_file`. Rules give
the middle ground.

Rules are read from `./.permissions.json` and `~/.localchat/permissions.json`;
rules from both files apply. Copy `.permissions.json.example` to start.

```json
{
  "allow": ["run_cmd(git status)", "mcp__filesystem__*"],
  "deny":  ["delete_file", "run_cmd(rm *)", "write_file(*/.env)"]
}
```

A rule is a tool name, optionally followed by a pattern in parentheses matched
against the call's main argument - the command for `run_cmd`, the path for a
file tool, the URL for a network tool. Both halves accept `*` and `?`. A pattern
with no wildcard also covers `<pattern> <anything>`, so `run_cmd(git status)`
already allows `git status --short`.

- **deny** does not run and does not ask. The tool's handler is never reached,
  and the model is told it is blocked so it stops retrying.
- **allow** runs without a prompt.
- Everything else asks, exactly as before - an empty rule set changes nothing.

### What a rule does not cover

A rule is matched against the call's argument **as text**, and it is worth being
plain about what that does and does not buy you.

**An allow rule stops at the command it names.** `run_cmd` runs its command
through a shell, so `git status && rm -rf ~` starts with the text
`run_cmd(git status)` allows. It is not allowed: an operator the rule itself
does not contain - `;` `&&` `||` `|` `` ` `` `$(` `${` `>` `<` - means the
command does more than the rule accounts for, and it falls through to the
approval prompt instead. A rule that asks for a pipeline outright, such as
`run_cmd(git log * | grep *)`, still gets one.

**A deny rule is a stop sign, not a sandbox.** It matches text, and text can be
rewritten: `run_cmd(rm *)` denies `rm -rf x` and does not recognise
`sh -c 'rm -rf x'` or `/bin/rm -rf x`. Those fall through to the approval
prompt, so the prompt is still between the model and the command - but with
`/automode on` there is no prompt, and then a deny rule is only as good as the
spelling the model happened to use. Deny is for the mistakes you expect, not for
an adversary.

**A pattern is never empty.** `write_file()` reads as "calls with no arguments"
and would have meant the opposite, so it is refused at load time and named in
`/perms`. Write the bare tool name, `write_file`, when you mean every call.

At an approval prompt the choices are now `[y/n/a]`, where `a` allows this
exact call from now on and appends the rule to `.permissions.json`. `/perms`
lists the active rules, `/perms allow <rule>` and `/perms deny <rule>` add one
by hand, and `/perms reload` re-reads the files.

---

## 13. Reasoning Models

Reasoning models (qwen3, deepseek-r1, gpt-oss) emit their scratch work before
the answer - either wrapped in `<think>` tags in the content stream, or in
Ollama's separate `thinking` field. It is not the answer, so:

- it is **not printed** (`/think on` shows it dimmed if you want to watch),
- it is **never stored** in the conversation history, which matters most: on a
  local model the context budget is small, and reasoning is often longer than
  the answer it produces.

Set `config.STORE_THINKING = True` to keep it in history anyway, or
`config.SHOW_THINKING = True` to have it shown from startup.

The same stream filter hides the `<tool_call>` tag, so a model that explains
itself and *then* calls a tool shows only the explanation.

---

## 14. Configuration

Most behaviour is reachable from a slash command, and those changes last for the
session. `config.py` is where you change what it starts as. The settings worth
knowing:

| Setting | Default | What it does |
| :--- | :--- | :--- |
| `MODEL` | `gemma4:e4b` | The Ollama model used until `/connect` says otherwise |
| `NUM_CTX` | 65536 | Context window asked of Ollama |
| `NUM_PREDICT` | 6144 | Output cap. Must be an int - every hosted API rejects a float |
| `NATIVE_TOOLS` | `True` | `False` forces the `<tool_call>` text protocol everywhere |
| `MAX_TOOL_CALLS` | 10 | Tool calls per turn before asking whether to continue |
| `AUTO_ALLOW` | `False` | `True` is `/automode on` from startup - no approval prompts |
| `PERMISSIONS_ENABLED` | `True` | Whether `.permissions.json` rules are consulted at all |
| `GIT_AUTO_COMMIT` | `True` | A commit per AI edit, so `/undo` has something to take back |
| `DEEPTHINK` | `False` | Start with the five-stage chain on |
| `SUBAGENT_MAX_TURNS` | 12 | Turns a sub-agent gets before it must report |
| `SUBAGENT_MAX_DEPTH` | 1 | 1 means sub-agents cannot hire sub-agents |
| `SHOW_THINKING` | `False` | Show a reasoning model's scratch work |
| `STORE_THINKING` | `False` | Keep it in the history too. Expensive on a local model |
| `AUTO_TITLE` | `True` | Let the model name each new session |
| `SAVE_CHAT_HISTORY` | `True` | Write session files at all |
| `CMD_TIMEOUT` | 120 | Seconds before a runaway command is killed |
| `CMD_WAIT_TIMEOUT` | 8 | Silence before a command is called "probably waiting" |
| `MCP_ENABLED` | `True` | Attach MCP servers on startup |
| `SEARXNG_URL` | `""` | A self-hosted search instance to prefer over the public sources |

The rest are tuning knobs for search, MCP and command sessions; they are
documented in the sections above and commented where they are defined.

State that outlives a session lives outside `config.py`:

| Path | Holds |
| :--- | :--- |
Everything about *you* lives in one directory, `~/.localchat`. Everything about
*a project* is read from that project's own directory first, and from
`~/.localchat` second - so a repository can carry its own rules, servers and
skills, and they win.

| Path | Holds |
| :--- | :--- |
| `~/.localchat/providers.json` | The connected provider and any API keys typed at `/connect`. Owner-only on POSIX |
| `~/.localchat/sessions/*.json` | Conversation transcripts, named after the session title |
| `~/.localchat/memory.json` | The long-term key-value memory |
| `~/.localchat/history` | Input history for the prompt |
| `./.permissions.json`, then `~/.localchat/permissions.json` | Allow and deny rules |
| `./.mcp.json`, then `~/.localchat/mcp.json` | MCP server declarations |
| `./skills/`, then `~/.localchat/skills/` | Skills |

Set `LOCALCHAT_HOME` to put that directory somewhere else - two profiles, or a
throwaway one for trying something out.

Before 0.2.0 the sessions, the memory and the input history were written into
whatever directory the harness started in. If you have those, they are not read
any more and nothing has moved them; the harness names them at startup and
prints the one line that moves them across.

---

## 15. Slash Commands

The interactive terminal supports special slash commands to control options and inspect state:

| Command | Description |
| :--- | :--- |
| `/help` | Display the list of available commands |
| `/usage` | Render an ASCII chart of historical token consumption |
| `/model` | Show the connected provider and pick another of its models |
| `/models` | List the models the connected provider offers |
| `/clear` | Clear the terminal display and reset conversation history |
| `/sessions` | List saved conversation sessions, newest first, with their titles |
| `/load <id or title>` | Load and render a past conversation session, found by id or title |
| `/title` | Show the current session's title and id |
| `/title <name>` | Retitle the current session and rename its file to match |
| `/autotitle <on/off>` | Toggle letting the model name a new session after its first exchange |
| `/automode <on/off>` | Enable or disable approval prompts for tool execution |
| `/fullcontent <on/off>` | Toggle truncating large file displays |
| `/record <on/off>` | Toggle automatically recording chat history into session files |
| `/export [filename]` | Export current chat history into a Markdown file |
| `/system <prompt>` | Set a custom system persona or reset to default (`/system reset`) |
| `/planmode <on/off>` | Require a plan approval before file edits or complex work |
| `/skills` | List discovered skills with their descriptions and paths |
| `/skills reload` | Rescan the skill directories and refresh the system prompt |
| `/skill <name>` | Load a skill into the current conversation by hand |
| `/mcp` | Show every configured MCP server, its state, and what it exposes |
| `/mcp tools [server]` | Expand the tool list of one or every connected server |
| `/mcp resources [server]` | List the resources the servers expose |
| `/mcp reload` | Re-read the config files and reconnect every server |
| `/mcp connect <name>` | Reconnect a single server |
| `/mcp prompt <server> <name> [k=v]` | Run a prompt template the server offers |
| `/mcp <on/off>` | Attach or detach every MCP server for this session |
| `/connect [provider] [model]` | Connect a provider, or pick one interactively |
| `/connect status` | Show every provider and whether it is usable |
| `/perms` | Show the active tool permission rules |
| `/perms reload` | Re-read the permission rule files |
| `/perms allow <rule>` | Add an allow rule, e.g. `/perms allow run_cmd(git *)` |
| `/perms deny <rule>` | Add a deny rule |
| `/think <on/off>` | Show or hide a reasoning model's thinking |
| `/deepthink` | The plan-check-build-review-verify chain, and whether it is on |
| `/deepthink <on/off>` | Turn that chain on or off |
| `/undo` | Take back the last file change the AI committed |
| `/autocommit` | Whether AI edits are committed, and the recent AI commits |
| `/autocommit <on/off>` | Turn that on or off |
| `/exit` or `/quit` | Exit the application |

---

## 16. Architecture

The codebase is organized cleanly around the following components:

- **`ARCHITECTURE.md`**: How the codebase is put together - the turn's control flow, the data shapes, the invariants, and what to touch for a given change. Read that before editing; read this to use it.
- **`app.py`**: Event loop, slash command router, and system prompt composition.
- **`llm_client.py`**: The conversation loop - streaming a reply, parsing the tool calls out of it, running them. Knows nothing about which provider answered.
- **`tools.py`**: Tool implementations, and the table binding each one to its entry in `toolspec.py`.
- **`toolspec.py`**: What every built-in tool is - name, description, parameters. The system prompt is rendered from it and dispatch binds arguments through it, so the two cannot drift apart.
- **`subagent.py`**: `spawn_agent` - a second model, hired for one self-contained job, working in its own context and handing back only its report.
- **`skills.py`**: Skill discovery, frontmatter parsing, and on-demand loading.
- **`providers.py`**: The provider abstraction - Ollama, Anthropic, OpenAI, Gemini - and the saved connection.
- **`connect.py`**: The `/connect` flow.
- **`sse.py`**: Reading server-sent events without waiting for data that has not been sent. Shared by the providers and the MCP client.
- **`permissions.py`**: Permission rule loading, matching, and the allow/deny/ask decision.
- **`shell_session.py`**: Live commands - output draining, waiting-for-input detection, and the session registry.
- **`deepthink.py`**: The five-stage chain - the stage instructions, what each stage may do, and when the chain stops early.
- **`git_ops.py`**: A commit per AI edit, and the undo that makes it worth having.
- **`atomic.py`**: Writing a file so a crash cannot leave half of it behind. Used for sessions, memory, permission rules and the saved API keys.
- **`tests/test_platform.py`**: Checks the waiting-for-input detection on the machine it is run on. Worth running on any new machine, and especially on Windows - see below.
- **`tests/test_registry.py`**: Fails if the tool table, the system prompt and the handlers stop describing the same tools.
- **`tests/test_durability.py`**: Atomic writes (including killing a writer mid-write) and the token estimate.
- **`tests/test_deepthink.py`**: Stage sequencing, both early stops, and that the planning stages really cannot edit.
- **`tests/test_git_ops.py`**: Auto-commit and undo against real repositories - including that undo refuses when it would destroy something.
- **`tests/test_native_tools.py`**: Each provider's tool-call wire format, and that both protocols end up in the same place.
- **`tests/test_docs.py`**: Fails when README.md or ARCHITECTURE.md names something that is gone, or misses something that is new.
- **`tests/test_subagent.py`**: What a sub-agent may do, what it may not, and that only its report crosses back.
- **`tests/test_permissions.py`**: What an allow rule covers - and that it stops at the command it names, rather than at whatever the shell was told to run next.
- **`tests/test_paths.py`**: That nothing personal is written into whatever directory you started in, and that state from an older version is named rather than moved.
- **`tests/test_tool_parsing.py`**: Every shape a model wraps a tool call in, and every shape that must not be read as one.
- **`requirements-lock.txt`**: The exact dependency set the harness was tested against. `requirements.txt` gives the tested floors and a ceiling before the next breaking release.
- **`mcp_client.py`**: MCP transports (stdio / streamable HTTP / SSE), the JSON-RPC session, tool and resource calls, and the prompt section they are advertised in.
- **`websearch.py`**: Multi-source retrieval, page extraction, and BM25 reranking.
- **`context.py`**: Token budgeting, tool-result trimming, and context compression. The token estimate is script-aware and calibrates itself against the counts each provider reports.
- **`renderer.py`** / **`tui.py`**: Markdown rendering and the terminal chrome.
- **`session.py`**: Session save/load/list and the persistent memory store.
- **`systemprompt.py`**: The system prompt - the assistant's own instructions, plus the tool-protocol rules that `subagent.py` shares. The tool schemas themselves come from `toolspec.py`.
- **`skills/`**: Project-level skills. Personal skills live in `~/.localchat/skills/`.
- **`.permissions.json`**: Project-level tool permission rules (see `.permissions.json.example`). Personal ones live in `~/.localchat/permissions.json`.
- **`.mcp.json`**: Project-level MCP server declarations (see `.mcp.json.example`). Personal ones live in `~/.localchat/mcp.json`.
- **`memory.json`**: Key-value JSON storage backing the long-term memory system.
- **`sessions/`**: Session directory containing JSON transcript backups for conversation history. Each file is named after the session's title (slugified, e.g. `웹-검색-랭킹-개선.json`); untitled sessions fall back to a timestamp until a title exists.
- **`.chat_history`**: History file managed by `prompt_toolkit` for command history recall across terminal runs.

---

## 17. License

Apache License 2.0 - see [LICENSE](LICENSE).

`pyproject.toml` holds the packaging metadata under the name `simple-harness`.
The modules live in `simple_harness/`, and the distribution installs that one
package rather than twenty-two top-level modules - which would otherwise put
`config`, `tools` and `session` in the importable root of every environment that
took it.

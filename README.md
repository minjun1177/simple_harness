# Local AI Terminal Client

## 1. What It Does

This application is a feature-rich, terminal-based AI assistant client built in Python for local Large Language Models running via Ollama. It transforms local LLMs into an autonomous workspace assistant equipped with function calling, interactive safety prompts, and real-time execution capabilities.

---

## 2. Features

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
- **Ollama**: Installed and running locally (default endpoint: `http://localhost:11434`)

### Installation Steps

1. **Install Required Packages**:
   ```bash
   pip install -r requirments.txt
   ```

2. **Pull an Ollama Model**:
   ```bash
   ollama pull gemma4:e4b
   ```

3. **Launch the Application**:
   ```bash
   python app.py
   ```

---

## 4. Tool Capabilities

The client equips the LLM with 25 function-calling tools divided into five core domain categories:

### Tool Call Format

The model calls a tool by emitting a `<tool_call>` block. Anything a parameter
can hold in one line goes in the JSON; a file body does not:

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

---

## 5. Web Search

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

## 6. Skills

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

---

## 7. MCP Servers

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

## 8. Running Commands

`run_cmd` captures the command's output, which means the command can never show
anything to the user while it runs - including a prompt. So it is given no
stdin at all (`DEVNULL`) and a deadline (`config.CMD_TIMEOUT`, 120s).

Without that, an interactive program inherited the terminal and sat waiting for
input, with its prompt captured and invisible: the whole app froze with nothing
on screen and no way to tell what it wanted. A command that simply runs too long
is now stopped along with everything it started, and whatever it printed first
is kept.

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

---

## 9. Tool Permissions

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

- **deny** never runs and never asks. The tool's handler is never reached, and
  the model is told it is blocked so it stops retrying.
- **allow** runs without a prompt.
- Everything else asks, exactly as before - an empty rule set changes nothing.

At an approval prompt the choices are now `[y/n/a]`, where `a` allows this
exact call from now on and appends the rule to `.permissions.json`. `/perms`
lists the active rules, `/perms allow <rule>` and `/perms deny <rule>` add one
by hand, and `/perms reload` re-reads the files.

---

## 10. Reasoning Models

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

## 11. Slash Commands

The interactive terminal supports special slash commands to control options and inspect state:

| Command | Description |
| :--- | :--- |
| `/help` | Display the list of available commands |
| `/usage` | Render an ASCII chart of historical token consumption |
| `/model` | View active model details or open an interactive model selection menu |
| `/models` | List all locally pulled Ollama models and their disk footprints |
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
| `/perms` | Show the active tool permission rules |
| `/perms reload` | Re-read the permission rule files |
| `/perms allow <rule>` | Add an allow rule, e.g. `/perms allow run_cmd(git *)` |
| `/perms deny <rule>` | Add a deny rule |
| `/think <on/off>` | Show or hide a reasoning model's thinking |
| `/exit` or `/quit` | Exit the application |

---

## 12. Architecture

The codebase is organized cleanly around the following components:

- **`app.py`**: Event loop, slash command router, and system prompt composition.
- **`ollama_client.py`**: Streaming chat turns, tool-call parsing, and the agent loop.
- **`tools.py`**: Tool implementations and the dispatch table.
- **`skills.py`**: Skill discovery, frontmatter parsing, and on-demand loading.
- **`permissions.py`**: Permission rule loading, matching, and the allow/deny/ask decision.
- **`shell_session.py`**: Live commands - output draining, waiting-for-input detection, and the session registry.
- **`tests/test_platform.py`**: Checks the waiting-for-input detection on the machine it is run on. Worth running on any new machine, and especially on Windows - see below.
- **`mcp_client.py`**: MCP transports (stdio / streamable HTTP / SSE), the JSON-RPC session, tool and resource calls, and the prompt section they are advertised in.
- **`websearch.py`**: Multi-source retrieval, page extraction, and BM25 reranking.
- **`context.py`**: Token budgeting, tool-result trimming, and context compression.
- **`renderer.py`** / **`tui.py`**: Markdown rendering and the terminal chrome.
- **`session.py`**: Session save/load/list and the persistent memory store.
- **`systemprompt.py`**: System prompt builder, tool specification schemas (in JSON format), and context summarization prompt definitions.
- **`skills/`**: Project-level skills. Personal skills live in `~/.localchat/skills/`.
- **`.permissions.json`**: Project-level tool permission rules (see `.permissions.json.example`). Personal ones live in `~/.localchat/permissions.json`.
- **`.mcp.json`**: Project-level MCP server declarations (see `.mcp.json.example`). Personal ones live in `~/.localchat/mcp.json`.
- **`memory.json`**: Key-value JSON storage backing the long-term memory system.
- **`sessions/`**: Session directory containing JSON transcript backups for conversation history. Each file is named after the session's title (slugified, e.g. `웹-검색-랭킹-개선.json`); untitled sessions fall back to a timestamp until a title exists.
- **`.chat_history`**: History file managed by `prompt_toolkit` for command history recall across terminal runs.
# ARCHITECTURE

For anyone - person or model - about to change this codebase. It says where
things are, what may not be broken, and what to touch for the change you have in
mind. `README.md` says what the harness does and how to use it; this says how it
is put together.

Read §2 and §5 before editing anything. They are short and they are where the
mistakes are.

---

## 1. What this is

A terminal AI assistant, ~9,500 lines of Python, no framework. It talks to
Ollama, Anthropic, OpenAI and Gemini over plain HTTP (no vendor SDKs), gives the
model 28 tools, and runs them with the user's approval.

The design constraint that explains most of the odd decisions: **it has to work
with a 4-billion-parameter local model.** Such a model cannot reliably escape a
source file into a JSON string, cannot be trusted to follow "do not edit yet",
and will report success it did not verify. Wherever you see machinery that looks
like distrust of the model, that is what it is, and it is load-bearing.

---

## 2. One turn, end to end

The single most useful thing to know. Follow it once and most of the codebase
falls into place.

```
app.main()                                      app.py
│
├─ read a line from the user
├─ if it starts with "/" → slash command, continue the loop
│
├─ messages.append({"role": "user", "content": <what they typed>})
├─ save_session(messages, id)                   session.py
│
├─ config.DEEPTHINK ? deepthink.run(messages)   deepthink.py  → §7
│                   : chat_turn(messages)       llm_client.py
│                     └─ context.manage_context first
│
└─ chat_turn(messages)                          llm_client.py
   │
   │  loop until the model stops calling tools:
   │
   ├─ tools = native_tools() if the provider supports it else None
   ├─ text = await stream_reply(messages, tools=..., calls_out=[])
   │  │
   │  └─ providers.current().stream(messages, tools=...)     providers.py
   │     yields {"text":…} {"thinking":…} {"tool_call":…} {"done":…}
   │     · text is printed through StreamFilter (hides <tool_call>, <think>)
   │     · thinking is printed only if SHOW_THINKING, never stored
   │     · tool_call events land in calls_out
   │     · done carries the token counts → context.observe_usage
   │
   ├─ calls = _from_native(calls_out) if calls_out
   │          else parse_tool_calls(text)          ← the JSON repair engine
   │
   ├─ if no calls → return the text. The turn is over.
   │
   └─ for (name, arguments) in calls:
      ├─ result = tools.dispatch_tool(name, arguments)        tools.py  → §4
      └─ messages.append({"role": "user",
                          "content": f"[Tool Result for '{name}']:\n{result}"})
      loop
```

Two things to notice, because a lot depends on them:

**A tool result is a `user` message, not a `tool` message.** The history is a
flat list of `{"role", "content"}` and nothing else, whatever the provider.

**A native tool call is written back into the history as `<tool_call>` text**
(`llm_client._render_call`). So a session recorded against Anthropic replays
against Ollama, the summariser and token estimator only ever see one format, and
`session.py` needs no notion of tool calls at all.

---

## 3. The data shapes

Four shapes carry everything. Keep them exactly.

**Message** — the only history format. There is no other.

```python
{"role": "system" | "user" | "assistant", "content": str}
```

`content` is always a plain string. A tool result is a user message beginning
`[Tool Result for '<name>']:\n`. `messages[0]` is always the system message.

**Stream event** — what every provider yields, whatever it speaks.

```python
{"text": str}                    # answer text, as it arrives
{"thinking": str}                # scratch work; never enters history
{"tool_call": {"name": str, "id": str, "arguments": dict,
               "error": str}}    # "error" only when the arguments would not parse
{"done": True, "prompt_tokens": int, "completion_tokens": int,
 "total_seconds": float, "eval_seconds": float}
```

A provider may yield any of these in any order. `done` comes last.

**Tool call** — what dispatch takes, from either protocol.

```python
(name: str, arguments: dict)
```

**Tool spec** — `toolspec.Tool` and `toolspec.Param`. One table, two renderings:
`prompt_schema()` for the text protocol, `native_schema()` for the real one.

---

## 4. `dispatch_tool` - every tool call goes through here

`tools.dispatch_tool(name, arguments)` in this order. Anything that must apply
to *all* tools belongs here and nowhere else.

1. **Normalise** — a string `arguments` is JSON-parsed; anything else becomes `{}`.
2. **Display** — `_fmt_tool_call`.
3. **Deepthink read-only gate** — refuses a world-changing tool during a
   planning stage (§7).
4. **Permission rules** — `deny` returns without reaching the handler; `allow`
   sets `config.POLICY_AUTO_ALLOW` so the handler's approval prompt passes.
5. **Run** — `_run_tool` looks the name up in `toolspec`, binds the arguments
   through it, calls the handler. MCP tools and the two MCP resource tools are
   handled after that lookup fails.
6. **Auto-commit** — `_commit_if_changed` commits a file the tool changed, if
   the tool reported `[Success`.

A refusal from step 3 or 4 returns a string starting with `[System]`. That
prefix is a contract: `chat_turn` counts consecutive `[System]` results and ends
the turn after three, so a model cannot spend its whole budget on a closed door.

---

## 5. Invariants

Each of these was a bug once. Breaking one is silent.

**5.1 The tool table is the only tool list.** `toolspec.TOOLS` generates the
system prompt *and* binds dispatch arguments. `tools._check_registry` raises at
first dispatch if a tool has no handler, a handler is undescribed, or a
handler's arity does not match its spec. Never hardcode a tool list anywhere
else.

**5.2 `config.py` builds the system prompt at import time** (`SYSTEM_PROMPT = syp()`).
So everything `systemprompt.py` reaches at module level - `toolspec`,
`mcp_client`, `skills` - **must not import `config` at module level.** They read
it lazily inside functions instead. Add a module-level `import config` to any of
them and the whole program stops importing.

**5.3 History is plain text, always.** No structured tool blocks, no
provider-specific shapes. Native tool calls are rendered back to `<tool_call>`
text before being stored. Break this and session files, `/load`, context
compression and the token estimate all need a second code path.

**5.4 Thinking never enters history.** `strip_thinking` runs before the
assistant message is appended, unless `STORE_THINKING`. On a local model the
reasoning is often longer than the answer, and the context budget is small.

**5.5 Ollama keeps the text protocol when its model cannot do better.**
`OllamaProvider.supports_native_tools` is a per-model property, not a class
attribute. A model whose template cannot format a tool call will never make one;
the text protocol plus the JSON repair engine is the only thing that works for
it. Anything that fails - daemon down, model unknown - means text.

**5.6 Tool-call repair never invents an argument.** `llm_client` will close
brackets a model left open, but a reply that *stopped mid-value* is reported as
unparseable instead. Guessing there would let `write_file` write an empty string
over a real file.

The envelope is repaired on the same terms. A model with no tool-calling
template writes the call in a markdown fence, or names it `tool_name`, or nests
it under a `tool_call` key - all of which used to parse to nothing, ending the
turn with no tool run and no error. `_fenced_tool_calls`, `_rename_to_name` and
`_unwrap_envelope` read those, and each is deliberately narrow: a fence is only
considered when the reply holds no `<tool_call>` at all, an unlabelled fence has
to decode to a tool that exists, and a lone key is only an envelope when its
only value is a dict carrying a name. Widening any of them turns an answer's
code block into a tool call.

**5.6a An allow rule stops at the command it names.** `run_cmd` goes through a
shell, and rules match text, so `run_cmd(git status)` also matches
`git status && rm -rf ~`. `permissions.chains_a_second_command` downgrades that
to the prompt. It never touches `deny`, and it never turns an `ask` into
anything stricter. An empty pattern - `write_file()` - is rejected at load
rather than matching everything, which is what it used to do.

**5.7 Files that must survive a crash are written through `atomic.py`.**
Sessions, memory, permission rules, saved API keys. `open(path, "w")` truncates
before it writes; being killed in between empties the file. Use
`atomic.write_json(path, data, private=True)` for anything holding a secret -
`private` keeps it owner-only for the whole write, which `open()` then `chmod`
does not. POSIX only: Windows has no mode bits, so `private` buys nothing there
and `test_durability.py` says so instead of asserting a "600" that cannot
exist.

**5.8 Auto-commit takes only the paths the tool named.** `git commit` is given
those paths explicitly. Never let it sweep up the index - the user's staged work
is not ours to commit.

**5.9 `[System]` prefixes a refusal.** See §4.

**5.10 Never report success you did not verify.** This applies to the code as
much as to the model: `deepthink._report_checks` counts the commands the final
stage actually ran, from the tool results, and prints that *after* the model's
prose - because the model will claim success it did not earn, and the reader
believes the last line.

---

## 6. Module map

Every module lives in `simple_harness/`, and imports it by the package name:
`from simple_harness import config`, `from simple_harness.tui import _hr`. The
tree below leaves that prefix off for readability; on disk `app.py` is
`simple_harness/app.py`. Tests and the two documents sit outside the package.

Layered by what may import what. A module may import anything above it.

```
app.py            the loop, slash commands, session lifecycle
  └ llm_client.py chat_turn / stream_reply / the JSON repair engine
      └ tools.py  dispatch_tool + every tool handler
      └ context.py token budget, trimming, compression
  └ deepthink.py  the five-stage chain
  └ subagent.py   spawn_agent's own conversation loop
      └ providers.py  four wire formats → one event shape
          └ sse.py    server-sent events, read as they arrive
          └ atomic.py crash-safe writes
  └ config.py     every setting, plus SYSTEM_PROMPT built at import
      └ systemprompt.py  the prompt text, both protocol variants
          └ toolspec.py  the tool table (stdlib only, imports nothing local)
```

| File | Owns | Do **not** put here |
| :--- | :--- | :--- |
| `app.py` | The REPL, slash commands, session save/rename, prompt refresh | Anything a tool does |
| `llm_client.py` | The turn loop, stream filtering, tool-call parsing and repair | Provider wire formats |
| `providers.py` | HTTP, streaming, per-vendor tool encoding, saved connection | Anything about *which* tools exist |
| `toolspec.py` | Names, descriptions, parameters, both schema renderings | Handlers, imports of other modules |
| `tools.py` | Handlers, dispatch, approval prompts | Tool descriptions - those are in `toolspec` |
| `systemprompt.py` | Prompt text; `tool_rules(native)` shared with `subagent` | A second copy of anything |
| `deepthink.py` | Stage list, stage instructions, stage gating | Tool logic |
| `subagent.py` | The sub-agent's own loop and prompt | A second protocol |
| `git_ops.py` | Commit, undo, diff. Never raises | Anything not about git |
| `context.py` | Token estimate, trimming, compression | |
| `session.py` | Session files and long-term memory | |
| `permissions.py` | Rule loading and the allow/deny/ask decision | |
| `shell_session.py` | Live commands, waiting-vs-busy detection | |
| `mcp_client.py` | MCP transports, JSON-RPC, MCP tool schemas | |
| `websearch.py` | Retrieval, extraction, BM25 reranking | |
| `skills.py` | Skill discovery and loading | |
| `tui.py` / `renderer.py` | Terminal chrome and markdown | Decisions |
| `atomic.py`, `sse.py` | One job each. Stdlib only | |

---

## 7. Deepthink

`deepthink.run(messages)` replaces one `chat_turn` with five, each preceded by a
stage instruction appended as a user message. All five share the one
conversation.

```
1 plan     read, decide, change nothing      edits = False
2 check    argue against the plan             edits = False
3 build    carry it out                       edits = True
4 review   read the real git diff             edits = True
5 verify   run it, report what came back      edits = True
```

Three mechanisms make it more than a prompt:

- **`config.DEEPTHINK_READONLY`** is set for stages 1-2, and `dispatch_tool`
  refuses anything in `tools._CHANGES_THINGS`. Asking a model to hold off does
  not hold it off - a 4B model tried to edit fifteen times before this existed.
- **Stage 4 is handed `git_ops.diff_since(sha)`**, the real patch, not a request
  to recall what it changed. Without git it is told to re-read the files.
- **`_report_checks`** counts the commands stage 5 actually ran and prints the
  truth after the model's summary (invariant 5.10).

Two early exits: a plan with nothing to build ends the chain after stage 1
(`_needs_building`, which believes the `NO_PLAN_NEEDED` marker for free and
otherwise asks one short question about the plan text); a build that changed
nothing ends it after stage 3 rather than reviewing work that never happened.

---

## 8. Recipes

### Add a tool

1. `toolspec.py` — a `Tool(...)` entry in `TOOLS`. Parameters in the order the
   handler takes them. Set `optional=True` on anything a call may omit; set
   `block=True` if the value arrives in a raw `<name>` block; add
   `native_description` if the text and native protocols need different words.
2. `tools.py` — write `handle_<name>(...)` and add one line to `_handlers()`.
3. Nothing else. The prompt, the native schemas and dispatch all follow.
   If they do not agree, `_check_registry` raises on the first tool call.
4. If it changes files, add it to `_WRITES_FILES` (auto-commit) and
   `_CHANGES_THINGS` (deepthink's read-only stages).

### Add a provider

1. Subclass `providers.Provider`: `name`, `label`, `key_env`,
   `default_base_url`, `list_models()`, `stream()`.
2. `stream()` must yield the §3 event shape and nothing else.
3. Set `supports_native_tools` and implement `encode_tools()` if it has a
   function-calling interface; leave both alone if it does not.
4. Register it in `providers.PROVIDERS`.
5. `merge_runs()` if it rejects consecutive same-role messages, `split_system()`
   if it wants the system prompt as its own field. Both already exist.

### Add a slash command

`app.py`, in the command block before `if config.PLANMODE:`. Then add it to the
`SlashCommandCompleter` list in `app.py` and to the help table in `tui.py` -
a command in none of the three lists is a command nobody finds.

### Change what the model is told

`systemprompt.py`. Text used by both protocols goes in the shared body of
`tool_rules()`; text that differs goes in the `_TEXT_*` / `_NATIVE_*` pair. Never
write a second copy - `subagent.py` reads the same function.

---

## 9. Tests

No framework. Each file is a script that prints `[ok]` / `[FAIL]` lines and
exits non-zero on failure. Run them all:

```bash
for t in tests/*.py; do python "$t" || echo "FAILED: $t"; done
```

| File | Guards |
| :--- | :--- |
| `test_registry.py` | The tool table, the prompt and the handlers still describe the same tools (5.1) |
| `test_native_tools.py` | Each vendor's tool-call wire format; both protocols reach the same place (5.3, 5.5) |
| `test_deepthink.py` | Stage sequencing, both early exits, read-only really read-only (§7) |
| `test_git_ops.py` | Commit and undo against real repositories, including undo refusing (5.8) |
| `test_durability.py` | Atomic writes, including killing a writer mid-write; the token estimate (5.7) |
| `test_subagent.py` | What a sub-agent may do, and that only its report crosses back |
| `test_platform.py` | Waiting-for-input detection on *this* machine. Run it on any new one, especially Windows |
| `test_permissions.py` | What an allow rule covers, and the two ways one used to cover more than it said |
| `test_tool_parsing.py` | The text protocol's repair engine: the shapes it reads, and the ones it refuses |
| `test_docs.py` | That this file and `README.md` still describe the program that exists |

`test_docs.py` is why the two documents can be trusted: it fails if either names
a file, function or setting that is gone, if `README.md` misses a tool or a
slash command, or if a number stated here stops matching the code. Prose cannot
be generated from the source the way the tool schemas are, but it can be held to
it.

`test_platform.py` runs the whole suite twice on Linux, the second time with
`/proc` switched off - which is exactly the code Windows and macOS take.

---

## 10. Things that surprise people

- **`config.py` imports `systemprompt`**, not the other way round. See 5.2.
- **There is no `tool` message role.** Tool results are user messages.
- **`toolspec.py` imports nothing local** and must stay that way.
- **`git_ops._git` never raises.** No git installed is a "no", not a crash.
- **Ollama's tool support is per model.** `providers.OllamaProvider.supports_native_tools`
  is a property; reading it off the class gives you the property object, not a bool.
- **`stream_reply` returns text only.** Native tool calls come back through the
  `calls_out` list you pass in.
- **The token estimate calibrates itself** against `prompt_tokens` from each
  provider. There is no bundled tokenizer, deliberately: `cl100k_base` is not
  the tokenizer of any model this runs against.
- **Sub-agents and deepthink both run through `dispatch_tool`**, so permission
  rules apply to them exactly as to the assistant. Neither is a way around a
  `deny`.

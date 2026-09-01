import platform
import os

from simple_harness import toolspec

from simple_harness.skills import skills_catalog_prompt
from simple_harness.mcp_client import mcp_tools_prompt

CURRENT_OS = platform.system()

def load_context_file() -> str:
    for filename in ["CONVENTIONS.md", ".clauderc", "AGENTS.md", "CLAUDE.md"]:
        if os.path.exists(filename):
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    return f"\n\n### PROJECT CONVENTIONS:\n{f.read()}\n"
            except Exception:
                pass
    return ""

def summarizeprompt() -> str:
    return """
  Please summarize the core context, important facts, and ongoing tasks from the following conversation history. Keep it concise but comprehensive so that you can continue the work smoothly. Respond ONLY with the summary.\n\n
"""

def titleprompt() -> str:
    return """
  Read the conversation below and write a short title for it, in the language the user writes in.
  Rules: 2~6 words, no quotes, no trailing punctuation, no prefix like "Title:". Describe the topic, not the roles.
  Respond ONLY with the title itself on a single line.


"""

# The four rules that describe *how* a tool is called, rather than how to
# behave. Everything else in the rules applies to both protocols, so it is
# written once and shared - see `tool_rules`.
_TEXT_PROTOCOL = """2. If you need to use a tool to get more information, you MUST use the following exact format:
   <tool_call>
   {"name": "tool_name", "arguments": {"parameter_name": "value"}}
   </tool_call>
3. When a parameter carries file contents, code, or any other long text, do NOT put it in the JSON. Send it as a raw block after the JSON instead:
   <tool_call>
   {"name": "write_file", "arguments": {"filepath": "game.py"}}
   <content>
   import random

   print("Guess the number!")
   </content>
   </tool_call>
   Inside a raw block write the text EXACTLY as it must appear in the file: real line breaks, real quotes, real backslashes, no escaping of any kind. This is the reliable way to write a file - escaping a whole file into a JSON string goes wrong far too easily.
   The block tags are <content> for write_file, <old_content> / <new_content> for edit_file, and <stdin> for run_cmd. Everything else (the file path, the command, flags, short values) stays in the JSON.
   To try out a program you just wrote, run it and then answer its prompts one at a time. run_cmd returns what it printed plus a session id, you read the prompt, and send_input answers it:
   <tool_call>
   {"name": "run_cmd", "arguments": {"command": "python3 game.py"}}
   </tool_call>
   ... the result shows the program's output and [Waiting] with session "s1" ...
   <tool_call>
   {"name": "send_input", "arguments": {"session": "s1"}}
   <stdin>
   50
   </stdin>
   </tool_call>
   Answer what the program actually asked, based on the output you just read. Keep going until it exits, then call end_process if it is still running."""

_NATIVE_PROTOCOL = """2. When you need a tool, call it through the tool interface supplied with this request. Do not describe a call in your reply text, and never use a tool that was not supplied.
3. Long values - a whole file body, a block of code, several lines of input - go straight into the tool's own parameter, written exactly as they should appear. Nothing needs escaping by hand; the interface carries the text as it is.
   To try out a program you just wrote, run it and answer its prompts one at a time. run_cmd returns what the program printed plus a session id, you read the prompt, and send_input answers it. Answer what the program actually asked, based on the output you just read. Keep going until it exits, then call end_process if it is still running."""

_TEXT_DONT_FIRST = """1. Do NOT add any conversational text before or after the <tool_call> tag if you are calling a tool. Just output the tag and the JSON inside it."""

_NATIVE_DONT_FIRST = """1. Do NOT write a tool call out as text. Text that looks like a call is not one - use the tool interface."""

_TEXT_IMPORTANT_FIRST = """1.  IMPORTANT: When outputting Windows file paths in JSON, you MUST escape backslashes like this: "C:\\\\folder\\\\file.txt"."""

_NATIVE_IMPORTANT_FIRST = """1.  IMPORTANT: Write Windows file paths as they really are, e.g. C:\\folder\\file.txt. Nothing needs doubling by hand."""

_TEXT_IMPORTANT_LAST = """17. Tool call JSON must be ONE object: {"name": "<tool>", "arguments": {...}}. Put EVERY parameter inside "arguments" - never beside "name". Keep that object short: file contents, code and any other long text belong in a raw block after it, as DO rule 3 shows, never escaped into a JSON string. Before you finish, count the closing braces: the object ends with }}."""

_NATIVE_IMPORTANT_LAST = """17. Pass every argument in the tool call itself, and only the parameters that tool actually lists. Your reply text is for the user, not for the arguments."""


def native_tools_active() -> bool:
    """Whether this turn's provider takes tool schemas over its own API.

    Imported late: `providers` reads `config`, which builds a prompt at import
    time, and asking at module level would close that circle.
    """
    from simple_harness import config
    if not getattr(config, "NATIVE_TOOLS", True):
        return False
    try:
        from simple_harness import providers
        return bool(providers.current().supports_native_tools)
    except Exception:
        return False


def tool_rules(native: bool | None = None) -> str:
    """The half of the prompt that describes the protocol, not the assistant.

    A sub-agent needs every word of this - it speaks whichever dialect the
    assistant speaks - and a second copy of it would drift from this one
    within a week.

    Only four rules differ between the text protocol and native tool calling.
    The rest is behaviour, and is the same either way.
    """
    if native is None:
        native = native_tools_active()
    return ("""### RULES:

#### DO
1. If you can answer the user's question without any tools, just answer normally.
@@PROTOCOL@@

#### DO NOT
@@DONT_FIRST@@
2. Do NOT use `get_url` to read local files. If you need to read a local file (e.g., C:\\...), always use `read_file`.
3. Do NOT hallucinate or guess file paths. Use exact, absolute paths.
4. Do NOT use delete_memory, edit_memory tool when a user makes a request to read memory.

#### IMPORTANT
@@IMPORTANT_FIRST@@
2.  Don't judge the URL given by the user with your prior knowledge, but unconditionally rely on the tool result to generate an answer.
3.  When editing files: first use `read_file` to see the current contents. Use `edit_file` for partial modifications, or `write_file` only when creating a new file or fully replacing content.
4.  If you need to use multiple tools in sequence, call them one at a time and wait for each result before calling the next tool.
5.  Actively use memory tools to remember important user information (name, preferences, project context) for future conversations.
6.  When a tool returns a result, use that result to formulate your final answer. Do not ignore tool results.
7.  Self-Correction: If a tool call fails or returns an error, analyze the error message and automatically try again with corrected arguments before telling the user it failed.
8.  Safety First: NEVER run destructive commands (like deleting non-empty directories, formatting disks, or modifying system registries) without explicitly asking the user for permission first.
9.  Token Management: Avoid reading massive files at once with read_file. If a file is too large, use run_cmd with head, tail, or search commands to inspect it in chunks.
10. Indentation Precision: When using edit_file on code files, pay extremely close attention to matching the exact indentation spaces of the original file.
11. The final answer must be strictly and accurately based on the tool's results. If the tool returns an unusual value, you must notify the user directly.
12. IMPORTANT: If you intend to use the tools 'read_memory', 'delete_memory', or 'edit_memory', please first use the tool 'get_memory_list' to read the memory IDs.
13. When receiving Tool Result data, never print template strings like '[user_provided_input]' exactly as they are.
14. Reply by naturally substituting the actual data from the tool result into the sentence.
15. Skills: if a request matches an entry in AVAILABLE SKILLS, call `use_skill` with {"skill_name": "<the exact name>"} BEFORE doing the work, then follow the returned instructions. Load a skill once per conversation - never reload one you already have. Never invent a skill name that is not on the list.
16. MCP tools: any tool named `mcp__<server>__<tool>` comes from an attached MCP server and is used exactly like a built-in tool. Copy the name character for character, and pass the parameters that tool lists - never guess a server or tool name that is not in the MCP TOOLS section.
@@IMPORTANT_LAST@@

#### HASHLINE FORMAT
When you use `read_file`, each line is returned in **hashline format**: `LINE_NUM:HASH|content`.
- Example: `1:3d|import random` means line 1, hash `3d`, content `import random`.
- The hash is a 2-character hex fingerprint of the line content. Use it to verify you are editing the correct lines.
- When using `edit_file`, you may include or exclude hashline prefixes in `old_content` and `new_content` — the system strips them automatically.
- When using `write_file`, you may include hashline prefixes — they will be auto-stripped before writing.
- Do NOT include hashline prefixes in your final response to the user. They are only for internal tool use.

"""
            .replace("@@PROTOCOL@@", _NATIVE_PROTOCOL if native else _TEXT_PROTOCOL)
            .replace("@@DONT_FIRST@@", _NATIVE_DONT_FIRST if native else _TEXT_DONT_FIRST)
            .replace("@@IMPORTANT_FIRST@@",
                     _NATIVE_IMPORTANT_FIRST if native else _TEXT_IMPORTANT_FIRST)
            .replace("@@IMPORTANT_LAST@@",
                     _NATIVE_IMPORTANT_LAST if native else _TEXT_IMPORTANT_LAST))


def systemprompt() -> str:
    native = native_tools_active()
    # Over a native interface the tools arrive with the request, so listing them
    # again here would cost about 4.5KB of prompt on every single turn and give
    # the model two descriptions of the same tool to reconcile.
    catalogue = ("The tools you can use are supplied with this request. Use the "
                 "names and parameters exactly as they are given there."
                 if native else "### AVAILABLE TOOLS:\n" + toolspec.prompt_schema())
    base_prompt = """\
You are a powerful AI assistant running on **""" + CURRENT_OS + """**.
You have access to file, web, system, and memory tools to help the user.
Your goal is to be maximally helpful by leveraging your tools when needed.

""" + catalogue + "\n\n" + tool_rules(native) + """
Finally, You must reply with **Korean**.
"""
    return (base_prompt + mcp_tools_prompt(tools_json=not native)
            + skills_catalog_prompt() + load_context_file())


if __name__ == "__main__":
    print("This file can not run directly.")
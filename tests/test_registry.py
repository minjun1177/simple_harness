"""The tool table, the prompt and the handlers must describe the same tools.

That was the point of moving the tool list into `toolspec.py`: the model used to
be told about tools from a hand-written JSON array and the harness used to run
them from a separate chain of `if` branches, with nothing checking that the two
still agreed. These tests fail if they stop agreeing.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from simple_harness import config
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
# The local model may or may not support native tool calling, and this file is
# about the text prompt, so the protocol is pinned rather than assumed.
config.NATIVE_TOOLS = False

from simple_harness import systemprompt
from simple_harness import toolspec
from simple_harness import tools

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


print("--- the table and the handlers ---")
handlers = tools._handlers()
check("every described tool can be run",
      set(toolspec.names()) <= set(handlers),
      str(sorted(set(toolspec.names()) - set(handlers))))
check("every handler is described to the model",
      set(handlers) <= set(toolspec.names()),
      str(sorted(set(handlers) - set(toolspec.names()))))
check("the consistency check runs at bind time", tools._HANDLERS is not None)

for label, mutate in [
    ("a missing handler is caught", lambda h: h.pop("edit_file")),
    ("an undescribed handler is caught", lambda h: h.update({"ghost": lambda: ""})),
    ("a changed signature is caught", lambda h: h.update({"git_diff": lambda a, b: ""})),
]:
    broken = dict(handlers)
    mutate(broken)
    try:
        tools._check_registry(broken)
        check(label, False, "drift went unnoticed")
    except RuntimeError:
        check(label, True)

print("\n--- the prompt is rendered, not transcribed ---")
prompt = systemprompt.systemprompt()
blob = prompt[prompt.index("### AVAILABLE TOOLS:") + len("### AVAILABLE TOOLS:"):
              prompt.index("### RULES:")].strip()
try:
    listed = json.loads(blob)
    check("the tool array is valid JSON", True, f"{len(listed)} tools")
except json.JSONDecodeError as error:
    listed = []
    check("the tool array is valid JSON", False, str(error))

check("it lists exactly the tools in the table",
      [t["name"] for t in listed] == toolspec.names())
check("raw-block parameters stay out of the JSON schema",
      all("content" not in t["parameters"]
          for t in listed if t["name"] == "write_file"))
check("but the tool still says where the body goes",
      all("<content>" in t["description"] for t in listed if t["name"] == "write_file"))
check("the protocol rules are shared, not copied",
      systemprompt.tool_rules() in prompt)
check("rule 17 no longer contradicts DO rule 3",
      "escape it as JSON" not in prompt)

print("\n--- arguments bind the way the handlers expect ---")
cases = [
    ("run_cmd", {"command": "ls"}, ["ls", ""]),
    ("run_cmd", {"command": "ls", "input": "y"}, ["ls", "y"]),
    ("send_input", {"session": "s1", "text": "50"}, ["s1", "50"]),
    ("send_input", {"session": "s1", "stdin": ""}, ["s1", ""]),
    ("search_in_file", {"query": "x"}, ["x", False]),
    ("search_in_file", {"query": "x", "is_regex": True}, ["x", True]),
    ("edit_file", {"filepath": "a.py", "old_content": "a", "new_content": "b"},
     ["a.py", "a", "b"]),
    ("use_skill", {"skill": "design"}, ["design"]),
    ("get_user_input", {"questions": [{"question": "q"}]}, ["", [], [{"question": "q"}]]),
]
for name, arguments, expected in cases:
    got = toolspec.get(name).bind(arguments)
    check(f"{name}({', '.join(arguments)})", got == expected, f"got {got}")

check("an alias resolves to the real tool", toolspec.get("skill").name == "use_skill")
check("an unknown tool is None", toolspec.get("no_such_tool") is None)

print("\n--- the native function-calling schema comes from the same table ---")
native = toolspec.native_schema()
check("one schema per tool", len(native) == len(toolspec.TOOLS))
check("each is JSON-serialisable", bool(json.dumps(native)))
check("required parameters are marked",
      "filepath" in dict((s["name"], s["input_schema"]["required"])
                         for s in native)["read_file"])
check("optional ones are not",
      "is_regex" not in dict((s["name"], s["input_schema"]["required"])
                             for s in native)["search_in_file"])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("registry checks passed")

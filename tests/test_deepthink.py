"""The deepthink chain: five turns over one request, and when it stops early.

Driven by a scripted provider, so what is being checked is the harness's
sequencing rather than a model's judgement. The two stop conditions matter most:
a request that needs no work must not cost five turns, and a review stage must
not run when nothing was changed - a model asked to review nothing will happily
report on work that never happened.
"""
import asyncio
import io
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from simple_harness import config
config.MCP_ENABLED = False
config.SAVE_CHAT_HISTORY = False
config.AUTO_ALLOW = True
config.NATIVE_TOOLS = False

from simple_harness import deepthink
from simple_harness import git_ops
from simple_harness import providers

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


class Scripted(providers.Provider):
    """Replays fixed replies and records the instruction each turn arrived with."""
    name = "scripted"
    label = "Scripted"
    needs_key = False

    def __init__(self, replies):
        super().__init__({"model": "scripted-1"})
        self.replies = list(replies)
        self.prompts = []

    def ready(self):
        return ""

    gate_answer = "YES"          # what it says when asked if there is work to do

    async def stream(self, messages, max_tokens=None, tools=None):
        last = messages[-1]["content"]
        self.prompts.append(last)
        # The chain asks a yes/no question of its own between stages. A real
        # provider answers it; a mock that hands back the next stage reply
        # instead would put every later stage one out of step.
        reply = (self.gate_answer if "Answer with one word, YES or NO" in last
                 else (self.replies.pop(0) if self.replies else "done."))
        yield {"text": reply}
        yield {"done": True, "prompt_tokens": 1, "completion_tokens": 1,
               "total_seconds": 0.0, "eval_seconds": 0.0}


def drive(replies, request="파서를 고쳐줘"):
    saved = providers._active
    scripted = Scripted(replies)
    providers._active = scripted
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": request}]
    quiet, real = io.StringIO(), sys.stdout
    sys.stdout = quiet
    try:
        answer = asyncio.run(deepthink.run(messages))
    finally:
        sys.stdout = real
        providers._active = saved
    printed = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", quiet.getvalue())
    return answer, scripted, messages, printed


def stage_prompts(scripted):
    """Only the deepthink instructions, not the tool-result turns."""
    return [p for p in scripted.prompts if "[Deepthink " in p]


print("--- the shape of the chain ---")
check("six stages", len(deepthink.STAGES) == 6, str(len(deepthink.STAGES)))
check("in the order the user asked for",
      [s.key for s in deepthink.STAGES]
      == ["plan", "check", "build", "review", "revise", "verify"],
      str([s.key for s in deepthink.STAGES]))
check("the first two are forbidden from editing",
      not any(s.edits for s in deepthink.STAGES[:2]))
check("the plan stage says so in words",
      "Change NOTHING" in deepthink.STAGES[0].instruction)
check("the check stage is told to look for failure, not reassurance",
      "not to feel better" in deepthink.STAGES[1].instruction)
check("the verify stage is told to run it, not describe it",
      "Do not describe it working" in deepthink.STAGES[5].instruction)
check("it is off by default", deepthink.enabled() is False)

print("\n--- a full run outside a repository ---")
plain = tempfile.mkdtemp(prefix="deep-plain-")
os.chdir(plain)
git_ops._repo_root_cache.clear()

answer, scripted, messages, printed = drive(
    ["계획: parser.py를 고친다.", "검토 결과 계획은 유효하다.",
     "구현했다.", "변경을 검토했다. 1. 문제 없음.", "고칠 것이 없었다.",
     "테스트를 돌렸고 통과했다."])
prompts = stage_prompts(scripted)
check("every stage ran", len(prompts) == 6, f"{len(prompts)} stages")
check("each turn was told which stage it is",
      all(f"[Deepthink {i}/6" in p for i, p in enumerate(prompts, 1)),
      str([p[:16] for p in prompts]))
check("the answer is the last stage's", answer.strip() == "테스트를 돌렸고 통과했다.",
      repr(answer))
check("all six are one conversation",
      sum(1 for m in messages if m["role"] == "assistant") == 6)
check("each stage is labelled on screen",
      all(f"deepthink {i}/6" in printed for i in range(1, 7)))
check("without git the review stage is told to re-read the files",
      "Reviewing from memory finds nothing" in prompts[3])

print("\n--- a request that needs no work stops after the plan ---")
answer, scripted, messages, printed = drive(
    [f"그건 이미 되어 있다. 확인해봤다.\n{deepthink.STOP_MARKER}",
     "should never run", "should never run"],
    request="이 프로젝트가 무슨 일을 하지?")
check("only one stage ran", len(stage_prompts(scripted)) == 1,
      str(len(stage_prompts(scripted))))
check("the marker is not shown to the user", deepthink.STOP_MARKER not in answer,
      repr(answer))
check("but the answer is kept", "이미 되어 있다" in answer, repr(answer))
check("and it says why it stopped", "nothing to build here" in printed)

print("\n--- inside a repository the review stage gets the real diff ---")
root = tempfile.mkdtemp(prefix="deep-git-")
subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root)
subprocess.run(["git", "config", "user.email", "t@t"], cwd=root)
subprocess.run(["git", "config", "user.name", "t"], cwd=root)
with open(os.path.join(root, "parser.py"), "w") as f:
    f.write("def parse(text):\n    return text\n")
subprocess.run(["git", "add", "-A"], cwd=root)
subprocess.run(["git", "commit", "-qm", "initial"], cwd=root)
os.chdir(root)
git_ops._repo_root_cache.clear()
config.GIT_AUTO_COMMIT = True

build = ('<tool_call>\n{"name": "edit_file", "arguments": {"filepath": "parser.py"}}\n'
         "<old_content>\n    return text\n</old_content>\n"
         "<new_content>\n    return text.strip()\n</new_content>\n</tool_call>")
answer, scripted, messages, printed = drive(
    ["계획을 세웠다.", "검토했다.", build, "구현 완료.",
     "diff를 검토했다.", "고칠 것이 없었다.", "테스트 통과."])
prompts = stage_prompts(scripted)
check("all six stages ran", len(prompts) == 6, str(len(prompts)))
check("the tool actually ran during the build stage",
      open(os.path.join(root, "parser.py")).read().strip().endswith("text.strip()"),
      repr(open(os.path.join(root, "parser.py")).read()))
review = prompts[3]
check("the review stage is handed a real patch", "```diff" in review)
check("the patch is of what changed",
      "+    return text.strip()" in review and "-    return text" in review,
      review[review.find("```diff"):][:120])
check("so it is not asked to review from memory",
      "Reviewing from memory" not in review)

print("\n--- a build that changed nothing skips the review ---")
config.GIT_AUTO_COMMIT = True
answer, scripted, messages, printed = drive(
    ["계획.", "검토.", "고칠 것이 없었다.", "최종 확인."])
prompts = stage_prompts(scripted)
check("the review stage is skipped", [p[:13] for p in prompts]
      == ["[Deepthink 1/", "[Deepthink 2/", "[Deepthink 3/"],
      str([p[:14] for p in prompts]))
check("and the chain stops there rather than verifying nothing",
      "nothing was changed" in printed)

print("\n--- with auto-commit off it still runs, just without a diff ---")
config.GIT_AUTO_COMMIT = False
answer, scripted, messages, printed = drive(
    ["계획.", "검토.", "구현.", "검토했다.", "고칠 것 없음.", "확인했다."])
prompts = stage_prompts(scripted)
check("all six stages still run", len(prompts) == 6, str(len(prompts)))
check("the review stage falls back to re-reading",
      "Reviewing from memory finds nothing" in prompts[3])
config.GIT_AUTO_COMMIT = True



print("\n--- a plan with nothing to build stops the chain even without the marker ---")
os.chdir(plain)
git_ops._repo_root_cache.clear()

class SaysNo(Scripted):
    gate_answer = "NO"


saved = providers._active
providers._active = SaysNo(
    ["이 함수는 평균을 냅니다. 바꿀 것은 없습니다.", "should never run"])
messages = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "mean 함수가 무슨 일을 하지?"}]
quiet, real = io.StringIO(), sys.stdout
sys.stdout = quiet
try:
    answer = asyncio.run(deepthink.run(messages))
finally:
    sys.stdout = real
    scripted = providers._active
    providers._active = saved
printed = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", quiet.getvalue())
check("the plan is read back when the marker is missing",
      any("Answer with one word" in p for p in scripted.prompts))
check("and the chain stops on it", len(re.findall(r"◆ deepthink \d/6", printed)) == 1,
      str(re.findall(r"◆ deepthink \d/6", printed)))
check("the answer is still the plan stage's", "평균을 냅니다" in answer, repr(answer[:40]))

providers._active = Scripted(["parser.py를 고치겠다.", "검토했다.", "구현.", "검토.",
                              "고칠 것 없음.", "확인."])
messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "고쳐줘"}]
quiet, real = io.StringIO(), sys.stdout
sys.stdout = quiet
try:
    asyncio.run(deepthink.run(messages))
finally:
    sys.stdout = real
    providers._active = saved
printed = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", quiet.getvalue())
check("a plan with work in it carries on", len(re.findall(r"◆ deepthink \d/6", printed)) == 6,
      str(re.findall(r"◆ deepthink \d/6", printed)))

async def _gate(text):
    return await deepthink._needs_building(text)

class Waffles(Scripted):
    gate_answer = "not a yes or no"


saved = providers._active
providers._active = Waffles([])
check("an unreadable verdict counts as work to do", asyncio.run(_gate("some plan")))
providers._active = saved


print("\n--- the harness reports what the final check really did ---")
os.chdir(plain)
git_ops._repo_root_cache.clear()
config.GIT_AUTO_COMMIT = False

FAILING = ('<tool_call>\n{"name": "run_cmd", "arguments": {"command": "exit 1"}}\n</tool_call>')

# A model that runs a command, is told it failed, and then says it all works.
answer, scripted, messages, printed = drive(
    ["계획.", "검토.", "구현.", "검토했다.", "고칠 것 없음.",
     FAILING, "테스트를 돌렸고 전부 통과했습니다. 완벽합니다."])
check("a failed check is called out regardless of the prose",
      "every command the final check ran failed" in printed, printed[-300:])
check("the model's own claim is left standing, not edited",
      "완벽합니다" in answer)

# A model that never ran anything at all.
answer, scripted, messages, printed = drive(
    ["계획.", "검토.", "구현.", "검토했다.", "고칠 것 없음.", "확인해보니 잘 동작합니다."])
check("a check that ran nothing is called out",
      "ran no command" in printed, printed[-200:])

# And a check that actually passed says nothing extra.
PASSING = ('<tool_call>\n{"name": "run_cmd", "arguments": {"command": "true"}}\n</tool_call>')
answer, scripted, messages, printed = drive(
    ["계획.", "검토.", "구현.", "검토했다.", "고칠 것 없음.", PASSING, "통과했습니다."])
check("a check that passed is not second-guessed",
      "final check ran" not in printed and "ran no command" not in printed,
      printed[-200:])
config.GIT_AUTO_COMMIT = True

check("the verify stage is told to check the request, not just the old tests",
      "were written before your change" in deepthink.STAGES[5].instruction)
check("the verify stage is told a broken command is not a result",
      "are not results" in deepthink.STAGES[5].instruction)
check("and not to claim success it did not earn",
      "do not follow that with a summary saying it works"
      in deepthink.STAGES[5].instruction)
check("the verify stage is told to check it against the plan",
      "back to the plan" in deepthink.STAGES[5].instruction)
check("the review stage is told to look for callers",
      "search_in_file" in deepthink.STAGES[3].instruction)

# Finding and fixing are separate stages: a stage allowed to fix stops looking
# as soon as it has something to fix, which is why review used to miss the rest
# of its own list.
check("the review stage is forbidden from fixing",
      "Do not fix anything here" in deepthink.STAGES[3].instruction)
check("and is asked for a list the next stage can work through",
      "numbered list" in deepthink.STAGES[3].instruction)
check("the revise stage works through that list",
      "list from the review" in deepthink.STAGES[4].instruction)
check("and is told not to widen it",
      "not a second review" in deepthink.STAGES[4].instruction)
check("an empty list means changing nothing",
      "change nothing" in deepthink.STAGES[4].instruction)

print("\n--- the planning stages cannot edit, whatever the model tries ---")
os.chdir(plain)
git_ops._repo_root_cache.clear()
from simple_harness import tools

config.DEEPTHINK_READONLY = True
quiet, real = io.StringIO(), sys.stdout
sys.stdout = quiet
try:
    refused = tools.dispatch_tool("write_file", {"filepath": os.path.join(plain, "no.py"),
                                                 "content": "should not exist"})
    ran_cmd = tools.dispatch_tool("run_cmd", {"command": "echo hi"})
    allowed = tools.dispatch_tool("git_status", {})
finally:
    sys.stdout = real
    config.DEEPTHINK_READONLY = False
check("a write is refused", "not available during this stage" in refused, refused[:60])
check("and nothing was written", not os.path.exists(os.path.join(plain, "no.py")))
check("a command is refused too - a command can write",
      "not available during this stage" in ran_cmd)
check("reading is still allowed", "not available during this stage" not in allowed)
check("the refusal tells it what to do instead", "say what you would" in refused)

# The stages that act must not be crippled by the same flag - and review is
# not one of them any more: it reports, revise fixes.
edits_allowed = [s.key for s in deepthink.STAGES if s.edits]
check("only build, revise and verify may edit",
      edits_allowed == ["build", "revise", "verify"], str(edits_allowed))
check("review cannot edit, so it cannot fix instead of finding",
      deepthink.STAGES[3].edits is False)

print("\n--- a model that keeps knocking is stopped ---")
from simple_harness import llm_client
knock = ('<tool_call>\n{"name": "edit_file", "arguments": {"filepath": "x.py"}}\n'
         "<old_content>\na\n</old_content>\n<new_content>\nb\n</new_content>\n</tool_call>")
config.DEEPTHINK_READONLY = True
saved = providers._active
providers._active = Scripted([knock] * 12 + ["알겠다, 계획만 쓰겠다."])
messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "고쳐줘"}]
quiet, real = io.StringIO(), sys.stdout
sys.stdout = quiet
try:
    answer = asyncio.run(llm_client.chat_turn(messages))
finally:
    sys.stdout = real
    providers._active = saved
    config.DEEPTHINK_READONLY = False
printed = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", quiet.getvalue())
nudges = [m for m in messages if "in a row you have been refused" in m["content"]]
check("it is told once that retrying will not help", len(nudges) == 1, str(len(nudges)))
check("and the turn ends rather than burning the budget",
      "ending the turn" in printed or "알겠다" in answer, repr(answer[:40]))
refused_calls = sum(1 for m in messages if "not available during this stage" in m["content"])
check("it did not get to try a dozen times", refused_calls <= 7, str(refused_calls))

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("deepthink checks passed")

"""Two things that only show up when something goes wrong.

* Files that must survive a crash mid-write. `open(path, "w")` truncates before
  it writes, so being killed in between leaves an empty file where a session
  transcript or an API key used to be.
* The context estimate. Under-counting is what silently overflows a context
  window, and the flat 3.5 characters-per-token this used to assume under-counts
  Korean badly.
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import atomic
import context

failures = []


def check(label, ok, extra=""):
    if not ok:
        failures.append(label)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}{f'  {extra}' if extra else ''}")


work = tempfile.mkdtemp(prefix="durability-")

print("--- atomic writes ---")
target = os.path.join(work, "session.json")
atomic.write_json(target, {"messages": ["안녕"], "n": 1})
check("it writes", json.load(open(target, encoding="utf-8"))["messages"] == ["안녕"])
check("non-ASCII survives", "안녕" in open(target, encoding="utf-8").read())

atomic.write_json(target, {"messages": ["두 번째"], "n": 2})
check("it replaces", json.load(open(target, encoding="utf-8"))["n"] == 2)
check("no temporary files left behind",
      [f for f in os.listdir(work) if f.startswith(".tmp-")] == [],
      str(os.listdir(work)))

# The value is serialised before anything is opened, so a value that cannot be
# encoded must leave the good file exactly as it was.
class Unserialisable:
    pass

try:
    atomic.write_json(target, {"bad": Unserialisable()})
    check("a bad value is refused", False, "it wrote anyway")
except TypeError:
    check("a bad value is refused", True)
check("and the old file is untouched",
      json.load(open(target, encoding="utf-8"))["n"] == 2)
check("still no temporaries", [f for f in os.listdir(work) if f.startswith(".tmp-")] == [])

secret = os.path.join(work, "providers.json")
atomic.write_json(secret, {"api_key": "sk-test"}, private=True)
check("a private file is owner-only", oct(os.stat(secret).st_mode)[-3:] == "600",
      oct(os.stat(secret).st_mode)[-3:])
check("an ordinary one is not", oct(os.stat(target).st_mode)[-3:] != "600",
      oct(os.stat(target).st_mode)[-3:])

# The real claim is that a reader never sees a half-written file. Kill the writer
# mid-write and check what a reader would have found.
victim = os.path.join(work, "victim.json")
atomic.write_json(victim, {"kept": "original"})
script = f'''
import os, sys, time
sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
import atomic
real = atomic.write_text
def slow(path, data, private=False):
    # Stall between writing the temporary file and swapping it in.
    import tempfile
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    os.write(fd, b"{{corrupt")
    os.close(fd)
    time.sleep(30)
    os.replace(tmp, path)
slow({victim!r}, "x")
'''
proc = subprocess.Popen([sys.executable, "-c", script])
import time as _time
_time.sleep(1.5)
mid_write = open(victim, encoding="utf-8").read()
proc.kill()
proc.wait()
check("a killed writer leaves the old file whole",
      json.loads(mid_write).get("kept") == "original", repr(mid_write[:40]))

print("\n--- the context estimate ---")
korean = [{"role": "user", "content": "안녕하세요. 이 하네스는 로컬 모델을 위한 도구 실행 환경입니다." * 8}]
english = [{"role": "user", "content": "This harness is a tool execution environment." * 12}]

flat = int(sum(len(m["content"]) for m in korean) / 3.5)
new = context._estimate_tokens(korean)
check("Korean is no longer counted as if it were English", new > flat * 1.3,
      f"flat 3.5 said {flat}, now {new}")
check("English is still counted sensibly",
      0 < context._estimate_tokens(english) < len(english[0]["content"]),
      str(context._estimate_tokens(english)))

wide = context._wide_chars("안녕 hello 漢字 かな")   # 2 Hangul + 2 Han + 2 Kana
check("wide scripts are counted apart from Latin", wide == 6, str(wide))

before = context._estimate_tokens(korean)
context.observe_usage(korean, int(before * 1.5))
after = context._estimate_tokens(korean)
check("a provider's real count moves the estimate", after > before,
      f"{before} -> {after}")
for _ in range(30):
    context.observe_usage(korean, int(context._raw_estimate(korean) * 1.5))
check("and it converges on it",
      abs(context._estimate_tokens(korean) - context._raw_estimate(korean) * 1.5) < 3,
      str(context._estimate_tokens(korean)))

settled = context._correction
context.observe_usage(korean, 999999)
check("a nonsense count is ignored", context._correction == settled)
context.observe_usage([{"role": "user", "content": "hi"}], 5000)
check("too short a sample is ignored", context._correction == settled)
context.observe_usage(korean, 0)
check("a provider that reports nothing is ignored", context._correction == settled)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("durability checks passed")

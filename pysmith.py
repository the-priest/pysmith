#!/usr/bin/env python3
"""
pysmith  -  AI-assisted Python toolsmith
========================================
Describe a tool in plain English. Groq writes it. You review it, run it,
tell it what's wrong, and iterate -- a conversational forge for building
Python / Kali / general CLI tooling, one tool at a time.

  - Zero dependencies. Standard library only.
  - AI code is NEVER auto-run. You see every line, then you choose to run it.
  - Nothing touches disk except the ./forge/ tools you explicitly /save.

Quick start:
    export GROQ_API_KEY="gsk_..."
    pysmith

Repo: https://github.com/the-priest/pysmith
License: MIT
"""

import os
import re
import io
import sys
import json
import time
import shutil
import token
import tokenize
import keyword
import tempfile
import threading
import subprocess
import urllib.request
import urllib.error

try:
    import readline  # arrow-key history / line editing in the prompt
except Exception:
    pass

__version__ = "1.0.0"

# ==========================================================================
# CONFIG  -- this is yours to edit
# ==========================================================================

# Groq biggest -> smallest fallback chain. pysmith tries each in order and
# falls through to the next on error / rate-limit.
# >>> Paste the exact model strings from your own verified chain here. <<<
# (Groq's catalogue shifts; these are reasonable defaults, not gospel.)
MODELS = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
]

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.join(os.getcwd(), "forge")   # where /save writes, in your CWD

SYSTEM_PROMPT = """You are pysmith, a Python toolsmith building small, self-contained
command-line tools for a security / sysadmin user on Kali Linux.

Rules:
- Output ONE complete, runnable Python script inside a single ```python fenced block.
- Prefer the standard library. If a third-party package is genuinely needed, say so in
  one short line BEFORE the code block, with the exact `pip install` command.
- If the tool wraps an external binary (nmap, hashcat, etc.), call it via subprocess and
  fail gracefully when the binary is missing.
- Provide an argparse interface where it makes sense, plus a one-line usage example in a
  comment at the top of the file.
- Keep prose tight: a one-line summary, then the code block, then at most two lines on how
  to run it.
- When asked to fix or change something, return the FULL updated script again, never a diff."""

DANGER = [
    r"rm\s+-rf\s+/", r":\(\)\s*\{", r"shutil\.rmtree\(\s*['\"]/", r"\bmkfs\b",
    r"dd\s+if=", r"os\.system\(\s*['\"]\s*rm\b", r">\s*/dev/sd", r"\bfork\(\)\s*while",
    r"shutil\.rmtree\(\s*os\.path\.expanduser",
]

BUILTINS = set(dir(__builtins__)) | {
    "self", "print", "open", "range", "len", "int", "str", "list", "dict",
    "set", "tuple", "enumerate", "zip", "map", "filter", "input",
}

# ==========================================================================
# terminal / colour
# ==========================================================================
TTY = sys.stdout.isatty()

class C:
    if TTY:
        R = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"; IT = "\033[3m"
        AMBER = "\033[38;5;179m"; EMBER = "\033[38;5;173m"
        LIME = "\033[38;5;149m"; CYAN = "\033[38;5;80m"
        RED = "\033[38;5;167m"; GREY = "\033[38;5;245m"; STEEL = "\033[38;5;252m"
        MAG = "\033[38;5;176m"
    else:
        R = B = DIM = IT = AMBER = EMBER = LIME = CYAN = RED = GREY = STEEL = MAG = ""

def cols():
    return max(54, min(shutil.get_terminal_size((96, 24)).columns, 110))

def rule(label="", col=C.GREY):
    w = cols()
    if label:
        head = f"{C.DIM}{col}\u2500\u2500{C.R} {C.B}{col}{label}{C.R} "
        pad = w - (len(label) + 4)
        print(head + f"{C.DIM}{col}" + "\u2500" * max(0, pad) + C.R)
    else:
        print(f"{C.DIM}{col}" + "\u2500" * w + C.R)

def wrap(text, indent="  "):
    w = cols() - len(indent)
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        line = ""
        for word in para.split(" "):
            if len(line) + len(word) + 1 > w:
                out.append(indent + line); line = word
            else:
                line = (line + " " + word).strip()
        out.append(indent + line)
    return "\n".join(out)

def say(msg, col=C.R):  print(f"{col}{msg}{C.R}")
def err(msg):           say("  \u2717 " + msg, C.RED)
def ok(msg):            say("  \u2713 " + msg, C.LIME)
def note(msg):          say("  " + msg, C.GREY)

# ==========================================================================
# python syntax highlighting (stdlib tokenize, fails safe to plain text)
# ==========================================================================
def _tok_colour(ttype, tstr):
    if ttype == tokenize.COMMENT:                 return C.GREY + C.IT
    if ttype == tokenize.STRING:                  return C.LIME
    if getattr(tokenize, "FSTRING_START", None) == ttype: return C.LIME
    if ttype == tokenize.NUMBER:                  return C.CYAN
    if ttype == tokenize.NAME:
        if keyword.iskeyword(tstr):               return C.AMBER + C.B
        if tstr in ("True", "False", "None"):     return C.MAG
        if tstr in BUILTINS:                      return C.CYAN
    return None

def highlight(code):
    lines = code.split("\n")
    spans = {i: [] for i in range(len(lines))}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(code).readline):
            ttype, tstr, (sr, sc), (er, ec), _ = tok
            colr = _tok_colour(ttype, tstr)
            if not colr:
                continue
            if sr == er:
                spans[sr - 1].append((sc, ec, colr))
            else:
                spans[sr - 1].append((sc, len(lines[sr - 1]), colr))
                for r in range(sr, er - 1):
                    spans[r].append((0, len(lines[r]), colr))
                if 0 <= er - 1 < len(lines):
                    spans[er - 1].append((0, ec, colr))
    except Exception:
        return None
    rendered = []
    for i, line in enumerate(lines):
        sp = sorted(spans[i]); pos = 0; res = ""
        for a, b, colr in sp:
            if a < pos:
                continue
            res += line[pos:a] + colr + line[a:b] + C.R
            pos = b
        res += line[pos:]
        rendered.append(res)
    return rendered

def print_code(code, name="draft"):
    hl = highlight(code) or code.split("\n")
    rule(f"{name}", C.STEEL)
    width_n = len(str(len(hl)))
    for i, line in enumerate(hl, 1):
        gutter = f"{C.DIM}{i:>{width_n}}{C.R} {C.DIM}\u2502{C.R} "
        print("  " + gutter + line)
    rule("", C.STEEL)

# ==========================================================================
# spinner
# ==========================================================================
class Spinner:
    FRAMES = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
    def __init__(self, label):
        self.label = label; self._stop = False; self._t = None
    def __enter__(self):
        if TTY:
            self._t = threading.Thread(target=self._run, daemon=True); self._t.start()
        else:
            note(self.label)
        return self
    def _run(self):
        i = 0
        while not self._stop:
            f = self.FRAMES[i % len(self.FRAMES)]
            sys.stdout.write(f"\r  {C.AMBER}{f}{C.R} {C.GREY}{self.label}{C.R}")
            sys.stdout.flush(); time.sleep(0.08); i += 1
    def __exit__(self, *a):
        self._stop = True
        if self._t:
            self._t.join()
            sys.stdout.write("\r" + " " * (len(self.label) + 6) + "\r"); sys.stdout.flush()

# ==========================================================================
# Groq call with fallback chain
# ==========================================================================
def call_groq(messages, key):
    last = None
    for model in MODELS:
        body = json.dumps({"model": model, "temperature": 0.3,
                           "messages": messages}).encode()
        req = urllib.request.Request(
            GROQ_URL, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"], model
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:160]
            last = f"{model}: HTTP {e.code} {detail}"
            note(f"{C.DIM}{model} -> {e.code}, trying next in chain{C.R}")
            time.sleep(0.4)
        except Exception as e:
            last = f"{model}: {e}"
            note(f"{C.DIM}{model} -> {e}, trying next in chain{C.R}")
    raise RuntimeError("Whole chain failed. Last: " + str(last))

# ==========================================================================
# helpers
# ==========================================================================
def extract_code(text):
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return m.group(1).rstrip() if m else None

def strip_code(text):
    return re.sub(r"```.*?```", "", text, flags=re.S).strip()

def looks_dangerous(code):
    return [p for p in DANGER if re.search(p, code)]

def third_party_imports(code):
    std = getattr(sys, "stdlib_module_names", set())
    mods = set()
    for m in re.finditer(r"^\s*(?:import|from)\s+([a-zA-Z0-9_\.]+)", code, re.M):
        top = m.group(1).split(".")[0]
        if top and top not in std and top not in ("__future__",):
            mods.add(top)
    # crude allowlist of obvious-stdlib in case stdlib_module_names is empty
    obvious = {"os","sys","re","io","json","time","math","socket","subprocess",
               "argparse","itertools","collections","random","hashlib","base64",
               "struct","threading","datetime","pathlib","shutil","csv","urllib"}
    return sorted(mods - obvious)

# ==========================================================================
# the forge
# ==========================================================================
class Forge:
    def __init__(self, key):
        self.key = key
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.code = None
        self.name = "tool"
        self.last_run = ""

    def ask(self, user_text, label="forging"):
        self.messages.append({"role": "user", "content": user_text})
        try:
            with Spinner(label + " \u2014 the model is thinking"):
                reply, model = call_groq(self.messages, self.key)
        except Exception as e:
            err(str(e)); self.messages.pop(); return
        self.messages.append({"role": "assistant", "content": reply})

        explanation = strip_code(reply)
        code = extract_code(reply)

        print()
        rule(f"{model}", C.CYAN)
        if explanation:
            print(wrap(explanation))
        if code:
            self.code = code
            self._name_from(user_text)
            print()
            print_code(code, self.name + ".py")
            self._hint()
        elif not explanation:
            note("(empty reply \u2014 try rephrasing)")

    def _hint(self):
        print(f"  {C.GREY}{C.B}/run{C.R}{C.GREY} test it  \u00b7  "
              f"{C.B}/fix <what broke>{C.R}{C.GREY} revise  \u00b7  "
              f"{C.B}/save{C.R}{C.GREY} keep it  \u00b7  "
              f"{C.B}/explain{C.R}{C.GREY} walk through it{C.R}")

    def _name_from(self, hint):
        skip = {"build","make","tool","that","with","this","write","create",
                "python","script","program","into","from","gimme","please","want"}
        for w in re.findall(r"[a-z][a-z0-9_]{3,}", hint.lower()):
            if w not in skip:
                self.name = w; return

    # ---- run ----
    def run(self, args):
        if not self.code:
            err("No draft yet. Describe a tool first."); return
        danger = looks_dangerous(self.code)
        if danger:
            say(f"\n  {C.RED}{C.B}!! destructive pattern(s): {', '.join(danger)}{C.R}", C.RED)
            if input(f"  {C.RED}type 'yes' to run anyway: {C.R}").strip().lower() != "yes":
                note("aborted."); return
        else:
            if input(f"  {C.AMBER}run this draft? [y/N] {C.R}").strip().lower() not in ("y","yes"):
                note("aborted."); return

        path = os.path.join(tempfile.gettempdir(), f"pysmith_{self.name}.py")
        with open(path, "w") as f:
            f.write(self.code)

        rule(f"$ python3 {self.name}.py {args}".rstrip(), C.AMBER)
        t0 = time.time()
        try:
            proc = subprocess.run([sys.executable, path] + (args.split() if args else []),
                                  capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            err("killed: exceeded 120s"); self.last_run = "TIMEOUT"; rule("", C.AMBER); return
        except Exception as e:
            err(f"could not launch: {e}"); return
        dt = time.time() - t0
        out = (proc.stdout or "") + (proc.stderr or "")
        self.last_run = out
        if out.strip():
            print("  " + out.rstrip().replace("\n", "\n  "))
        else:
            note("(no output)")
        rule("", C.AMBER)
        if proc.returncode == 0:
            ok(f"exit 0  \u00b7  {dt:.2f}s  \u00b7  clean")
        else:
            say(f"  \u2717 exit {proc.returncode}  \u00b7  {dt:.2f}s  \u00b7  "
                f"{C.B}/fix{C.R}{C.AMBER} sends this error back to the model{C.R}", C.AMBER)

    # ---- fix ----
    def fix(self, text):
        if not self.code:
            err("nothing to fix yet."); return
        parts = []
        if text: parts.append(text)
        if self.last_run.strip():
            parts.append("Output / error from the last run:\n" + self.last_run.strip()[-2000:])
        if not parts:
            err("say what to fix:  /fix it crashes on empty input"); return
        self.ask("Revise the current script. " + "\n\n".join(parts), label="reforging")

    def explain(self):
        if not self.code:
            err("no draft to explain."); return
        self.ask("Explain what the current script does, step by step but concisely. "
                 "Do not output code, just the explanation.", label="explaining")

    def deps(self):
        if not self.code:
            err("no draft yet."); return
        tp = third_party_imports(self.code)
        if not tp:
            ok("no third-party packages \u2014 pure standard library."); return
        say(f"  third-party imports detected: {C.B}{', '.join(tp)}{C.R}", C.AMBER)
        note(f"install with:")
        print(f"  {C.LIME}pip install {' '.join(tp)} --break-system-packages{C.R}")

    # ---- save / load ----
    def save(self, name):
        if not self.code:
            err("no draft to save."); return
        os.makedirs(TOOL_DIR, exist_ok=True)
        name = (name or self.name).strip().replace(" ", "_")
        if not name.endswith(".py"): name += ".py"
        path = os.path.join(TOOL_DIR, name)
        with open(path, "w") as f:
            f.write(self.code + "\n")
        try: os.chmod(path, 0o755)
        except Exception: pass
        ok(f"saved \u2192 {path}")

    def load(self, name):
        if not name:
            self.list_tools(); return
        if not name.endswith(".py"): name += ".py"
        path = os.path.join(TOOL_DIR, name)
        if not os.path.exists(path):
            err(f"not found: {path}"); self.list_tools(); return
        with open(path) as f:
            self.code = f.read()
        self.name = name[:-3]
        self.messages.append({"role": "user",
            "content": f"Existing tool I want to keep working on:\n```python\n{self.code}\n```"})
        self.messages.append({"role": "assistant", "content": "Loaded. What should change?"})
        ok(f"loaded {name}")
        print(); print_code(self.code, name)

    def list_tools(self):
        if not os.path.isdir(TOOL_DIR):
            note("no saved tools yet \u2014 /save writes into ./forge/"); return
        tools = sorted(f for f in os.listdir(TOOL_DIR) if f.endswith(".py"))
        if not tools:
            note("no saved tools yet."); return
        rule("forge/", C.LIME)
        for t in tools:
            sz = os.path.getsize(os.path.join(TOOL_DIR, t))
            print(f"  {C.LIME}\u2022{C.R} {t}  {C.DIM}{sz}b{C.R}")

# ==========================================================================
# chrome
# ==========================================================================
BANNER = r"""
                          _ _   _
 _ __  _   _ ___ _ __ ___ (_) |_| |__
| '_ \| | | / __| '_ ` _ \| | __| '_ \
| |_) | |_| \__ \ | | | | | | |_| | | |
| .__/ \__, |___/_| |_| |_|_|\__|_| |_|
|_|    |___/
"""

def banner():
    if TTY:
        print(C.EMBER + C.B + BANNER + C.R)
    say(f"  {C.B}{C.STEEL}pysmith{C.R} {C.DIM}v{__version__}{C.R}  "
        f"{C.GREY}\u2014 AI toolsmith \u00b7 Groq \u00b7 stdlib only \u00b7 no auto-run{C.R}")
    say(f"  {C.DIM}the-priest/pysmith{C.R}")

HELP = f"""
  {C.B}plain english{C.R}{C.GREY}   describe a tool to build, or chat to steer it
  {C.B}/run [args]{C.R}{C.GREY}     run the current draft (you confirm first)
  {C.B}/fix [text]{C.R}{C.GREY}     revise it; the last run's error is attached automatically
  {C.B}/explain{C.R}{C.GREY}        the model walks through the current draft
  {C.B}/deps{C.R}{C.GREY}           list any third-party imports + the pip line
  {C.B}/show{C.R}{C.GREY}           reprint the current draft
  {C.B}/save [name]{C.R}{C.GREY}    write the draft into ./forge/
  {C.B}/load [name]{C.R}{C.GREY}    load a saved tool to keep iterating (no name = list)
  {C.B}/new{C.R}{C.GREY}            start a fresh conversation
  {C.B}/model{C.R}{C.GREY}          show the model fallback chain
  {C.B}/help{C.R}   {C.B}/quit{C.R}
{C.R}"""

def main():
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        try:
            key = input("Groq API key (gsk_...): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not key:
        err("no key, no forge.  export GROQ_API_KEY=...  and rerun."); return

    banner()
    note("type a description to begin, or /help.  code is shown, never auto-run.")
    forge = Forge(key)

    while True:
        try:
            line = input(f"\n{C.EMBER}{C.B}\u2738 pysmith{C.R} {C.DIM}\u203a{C.R} ").strip()
        except EOFError:
            print(); break
        except KeyboardInterrupt:
            print(f"\n  {C.DIM}(ctrl-d or /quit to leave){C.R}"); continue
        if not line:
            continue

        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            cmd = cmd.lower().strip(); arg = arg.strip()
            if cmd in ("q","quit","exit"):              break
            elif cmd == "help":                          print(HELP)
            elif cmd == "run":                           forge.run(arg)
            elif cmd == "fix":                           forge.fix(arg)
            elif cmd == "explain":                       forge.explain()
            elif cmd == "deps":                          forge.deps()
            elif cmd == "show":
                if forge.code: print(); print_code(forge.code, forge.name + ".py")
                else: note("no draft yet.")
            elif cmd == "save":                          forge.save(arg)
            elif cmd == "load":                          forge.load(arg)
            elif cmd == "new":
                forge.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                forge.code = None; forge.last_run = ""; ok("fresh conversation.")
            elif cmd == "model":
                rule("model chain (biggest \u2192 smallest)", C.CYAN)
                for i, m in enumerate(MODELS, 1):
                    print(f"  {C.DIM}{i}{C.R} {m}")
                note("edit the MODELS list at the top of pysmith.py to change it")
            else:
                err(f"unknown command /{cmd}  \u2014  /help for the list")
        else:
            forge.ask(line)

    say(f"\n  {C.EMBER}forge banked. later.{C.R}\n", C.R)

if __name__ == "__main__":
    main()

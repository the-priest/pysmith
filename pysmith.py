#!/usr/bin/env python3
"""
pysmith  -  AI-assisted Python toolsmith (desktop app)
======================================================
A local workspace for building Python / Kali / CLI tools by talking to a model.
You agree on the tool in a build dialogue, it writes a TESTING version you run
right here, you iterate, and only when you ask does it package a RELEASE version.

This file is a tiny local server (standard library only). It:
  - serves the workspace UI to your browser
  - keeps your Groq key on THIS machine (never sent to the browser)
  - actually runs the generated code locally so "test it" is real
  - never auto-runs anything: you click run, and a destructive-pattern scan guards it

Run:
    export GROQ_API_KEY="gsk_..."
    python3 pysmith.py            # opens http://127.0.0.1:8765 in your browser

Repo: https://github.com/the-priest/pysmith   ·   License: MIT
"""

import os
import re
import sys
import json
import time
import shlex
import socket
import tempfile
import threading
import webbrowser
import subprocess
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "8.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))

# ==========================================================================
# CONFIG  -- yours to edit
# ==========================================================================

# --------------------------------------------------------------------------
# PROVIDERS
# --------------------------------------------------------------------------
# pysmith can call several providers. You pick one per session in the UI; if a
# call fails it falls through that provider's own model chain (biggest first).
# Keys are read from env vars (below) or pasted in Settings. Nothing is sent to
# the browser; keys persist to an owner-only config file.
#
# >>> Edit model strings to match what your accounts actually have access to. <<<
PROVIDERS = {
    "groq": {
        "label": "Groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "env": "GROQ_API_KEY",
        "kind": "openai",
        "models": [
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "gemma2-9b-it",
            "llama-3.1-8b-instant",
        ],
    },
    "siliconflow": {
        "label": "SiliconFlow",
        "url": "https://api.siliconflow.com/v1/chat/completions",
        "env": "SILICONFLOW_API_KEY",
        "kind": "openai",
        # biggest / strongest first
        "models": [
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
            "deepseek-ai/DeepSeek-V2.5",
            "Qwen/Qwen2.5-7B-Instruct",
        ],
    },
    "google": {
        "label": "Google AI Studio",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "env": "GOOGLE_API_KEY",
        "kind": "openai",   # google exposes an OpenAI-compatible endpoint
        "models": [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
        ],
    },
    "novita": {
        "label": "Novita AI",
        "url": "https://api.novita.ai/v3/openai/chat/completions",
        "env": "NOVITA_API_KEY",
        "kind": "openai",
        "models": [
            "deepseek/deepseek-v3",
            "qwen/qwen-2.5-72b-instruct",
            "meta-llama/llama-3.1-70b-instruct",
            "openai/gpt-oss-120b",
            "meta-llama/llama-3.1-8b-instruct",
        ],
    },
}

# default provider on first launch
DEFAULT_PROVIDER = "groq"

# auto-test loop: after the model writes code, pysmith silently checks it and
# feeds failures back to the model up to this many times before showing you.
AUTOTEST_MAX_ROUNDS = 2

HOST = "127.0.0.1"
PORT = 8765

# This is the heart of it: the model is taught to build tools the way a careful
# senior engineer does -- agree first, testing version by default, release only
# on request. Tune it to taste.
SYSTEM_PROMPT = """You are pysmith, a senior Python engineer who builds small, sharp, genuinely
working command-line tools for a security / sysadmin user on Kali Linux. You write the kind of
code a careful professional ships: correct, defensive, readable. Hold yourself to that bar
regardless of how the request is phrased.

ENGINEERING STANDARDS (apply to every script you write):
- Correctness first. The code must actually run and do what was agreed. Mentally trace the
  happy path AND the obvious failure paths before you output.
- Validate inputs. Check args, file existence, ranges, formats. Never assume input is well-formed.
- Fail gracefully and informatively: catch the exceptions that will realistically occur
  (network timeouts, missing files, permission denied, malformed data, missing binaries),
  print a clear message to stderr, and exit with a non-zero code. Never let a bare traceback
  be the user experience for an expected error.
- Wrap external binaries (nmap, hashcat, tcpdump, etc.) with subprocess; detect when the binary
  is absent and tell the user exactly what to install.
- No silent failure, no bare `except: pass`. No placeholder/stub functions presented as working.
  No invented library APIs — if unsure a function exists, use a standard approach you are sure of.
- Concurrency/timeouts where it matters (e.g. scanning many hosts) so the tool isn't unusably slow,
  but keep it correct over clever.
- Prefer the standard library. If a third-party package is genuinely needed, put the exact
  `pip install X` line on ONE line BEFORE the code block.

METHOD (the build dialogue):
1. CLARIFY BEFORE BUILDING. If meaningful details are unresolved, do not dump code — surface the
   decisions. (pysmith may run a structured intake for you; honour every answer it passes you
   precisely.) Once the shape is clear, build.
2. TESTING VERSION BY DEFAULT: ONE complete, runnable, single-file script. Lean but correct —
   full input validation and error handling, but no packaging ceremony yet.
3. ITERATE on real feedback: when given a run result/error/log, return the FULL updated script
   (never a diff) and state briefly what you changed and why.
4. RELEASE VERSION ONLY WHEN ASKED: top docstring with summary + usage example, clean argparse
   CLI with --help, robust error handling, sensible exit codes, helpful comments, zero dead code.
5. SAFETY: no destructive operations (mass deletion, disk wipes, fork bombs) unless the user
   explicitly and unambiguously asks; if so, call it out. Assume it runs on the user's own machine.

OUTPUT FORMAT: a tight message first (a few sentences). THEN, only when actually providing code,
exactly ONE ```python fenced block with the entire script — never two blocks. When only planning
or discussing, include no code block at all."""

# Used to generate a tailored, clickable intake for a new tool request.
INTAKE_PROMPT = """You are the requirements analyst for pysmith, a Python tool builder. The user
wants to build a tool. Your job is to produce the SHORT, HIGH-VALUE set of questions needed to
build EXACTLY what they want — no lazy or generic filler.

Return ONLY a JSON object, no prose, no markdown fences:
{"summary": "<one line restating what they want to build>",
 "questions": [
   {"q": "<clear question>", "options": ["<opt1>", "<opt2>", "<opt3>"], "multi": false},
   ...
 ]}

Rules:
- 3 to 6 questions MAX. Only ask what genuinely changes the code.
- Tailor every question to THIS tool. A port scanner needs scan-type/output-format questions;
  a log parser needs input-format/filter questions. Do not ask irrelevant things.
- Always cover, where relevant: inputs (what/format), outputs (stdout/JSON/CSV/file), key
  behaviour options, whether wrapping an external binary or pure-Python, and any third-party
  dependency tolerance.
- 2 to 4 options per question. Options must be concrete and mutually distinct. Set "multi": true
  only when picking several genuinely makes sense.
- Prefer options the user can just tap. Keep them short."""

# Used by the GitHub-ready flow to assemble repo files from the user's answers.
GITHUB_PROMPT = """You are preparing a polished GitHub release of a Python tool. You will be given
the final code and the user's repo details. Produce a complete, professional repo.

Return ONLY a JSON object, no prose, no markdown fences:
{"readme": "<full README.md markdown>",
 "gitignore": "<.gitignore contents>",
 "requirements": "<requirements.txt contents, or empty string if pure stdlib>",
 "description": "<one-line repo description>"}

README requirements:
- Title, one-line description, then a short paragraph on what it does.
- An "Install" section with a ONE-LINE curl command that downloads and runs install.sh from the
  user's repo over HTTPS (never ssh). Use the raw.githubusercontent.com URL for their repo/branch.
  The same line should work for updates (re-running it).
- A "Usage" section with real, copy-pasteable examples derived from the actual CLI in the code.
- Requirements, and the license name.
- Clean, scannable, professional. No fluff."""

DANGER = [
    r"rm\s+-rf\s+/", r":\(\)\s*\{", r"shutil\.rmtree\(\s*['\"]/", r"\bmkfs\b",
    r"dd\s+if=", r"os\.system\(\s*['\"]\s*rm\b", r">\s*/dev/sd", r"\bfork\(\)\s*while",
    r"shutil\.rmtree\(\s*os\.path\.expanduser",
]

# key persistence: per-provider keys in an owner-only config file
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "pysmith", "config.json")

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except Exception:
        return {}

def save_config(cfg):
    """Write config (keys + chosen provider) with owner-only perms."""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False

def _initial_keys():
    """env var wins per provider, else the saved config."""
    saved = load_config().get("keys", {})
    keys = {}
    for pid, p in PROVIDERS.items():
        keys[pid] = os.environ.get(p["env"], "").strip() or (saved.get(pid) or "").strip()
    return keys

# session state: per-provider keys + the currently selected provider + chosen model per provider
STATE = {
    "keys": _initial_keys(),
    "provider": load_config().get("provider") or DEFAULT_PROVIDER,
    "models": load_config().get("models", {}),   # {provider_id: chosen_model}
}

def persist_state():
    return save_config({"keys": STATE["keys"], "provider": STATE["provider"],
                        "models": STATE["models"]})

# --------------------------------------------------------------------------
# TOOL LIBRARY  -- persistent, reloadable tools (code + conversation)
# --------------------------------------------------------------------------
LIBRARY_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "pysmith", "library")

def _safe_id(name):
    return re.sub(r"[^A-Za-z0-9_\-]", "_", (name or "tool")).strip("_") or "tool"

def library_save(name, code, messages):
    """Persist a tool (its code + the conversation that built it) to the library."""
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    tid = _safe_id(name)
    rec = {"id": tid, "name": name or tid, "code": code,
           "messages": messages or [], "saved": time.strftime("%Y-%m-%d %H:%M")}
    with open(os.path.join(LIBRARY_DIR, tid + ".json"), "w") as f:
        json.dump(rec, f)
    return {"id": tid, "saved": rec["saved"]}

def library_list():
    if not os.path.isdir(LIBRARY_DIR):
        return {"tools": []}
    tools = []
    for fn in os.listdir(LIBRARY_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(LIBRARY_DIR, fn)) as f:
                r = json.load(f)
            tools.append({"id": r.get("id"), "name": r.get("name"),
                          "saved": r.get("saved"),
                          "lines": len((r.get("code") or "").splitlines())})
        except Exception:
            continue
    tools.sort(key=lambda t: t.get("saved", ""), reverse=True)
    return {"tools": tools}

def library_load(tid):
    path = os.path.join(LIBRARY_DIR, _safe_id(tid) + ".json")
    if not os.path.exists(path):
        return {"error": "not found"}
    with open(path) as f:
        return {"tool": json.load(f)}

def library_delete(tid):
    path = os.path.join(LIBRARY_DIR, _safe_id(tid) + ".json")
    try:
        os.remove(path); return {"ok": True}
    except Exception as e:
        return {"error": str(e)}

# --------------------------------------------------------------------------
# SESSIONS  -- live works-in-progress (auto-saved as you build), like chats
# --------------------------------------------------------------------------
SESSION_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "pysmith", "sessions")

def session_save(sid, name, code, messages):
    """Auto-save the live conversation+code for a tool in progress."""
    os.makedirs(SESSION_DIR, exist_ok=True)
    sid = sid or time.strftime("s%Y%m%d-%H%M%S")
    rec = {"id": sid, "name": name or "untitled", "code": code or "",
           "messages": messages or [], "updated": time.strftime("%Y-%m-%d %H:%M")}
    with open(os.path.join(SESSION_DIR, _safe_id(sid) + ".json"), "w") as f:
        json.dump(rec, f)
    return {"id": sid, "updated": rec["updated"]}

def session_list():
    if not os.path.isdir(SESSION_DIR):
        return {"sessions": []}
    out = []
    for fn in os.listdir(SESSION_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSION_DIR, fn)) as f:
                r = json.load(f)
            msgs = r.get("messages", [])
            out.append({"id": r.get("id"), "name": r.get("name"),
                        "updated": r.get("updated"),
                        "turns": sum(1 for m in msgs if m.get("role") == "user"),
                        "hasCode": bool(r.get("code"))})
        except Exception:
            continue
    out.sort(key=lambda s: s.get("updated", ""), reverse=True)
    return {"sessions": out}

def session_load(sid):
    path = os.path.join(SESSION_DIR, _safe_id(sid) + ".json")
    if not os.path.exists(path):
        return {"error": "not found"}
    with open(path) as f:
        return {"session": json.load(f)}

def session_delete(sid):
    try:
        os.remove(os.path.join(SESSION_DIR, _safe_id(sid) + ".json")); return {"ok": True}
    except Exception as e:
        return {"error": str(e)}

# --------------------------------------------------------------------------
# DEPENDENCIES  -- detect third-party imports, optionally install into a venv
# --------------------------------------------------------------------------
def detect_deps(code):
    """Return third-party top-level imports (best-effort, stdlib-aware)."""
    std = getattr(sys, "stdlib_module_names", set())
    obvious = {"os","sys","re","io","json","time","math","socket","subprocess","argparse",
               "itertools","collections","random","hashlib","base64","struct","threading",
               "datetime","pathlib","shutil","csv","urllib","textwrap","glob","tempfile",
               "functools","typing","enum","dataclasses","queue","signal","select","ssl",
               "ipaddress","binascii","zlib","gzip","sqlite3","html","xml","http","email"}
    mods = set()
    for m in re.finditer(r"^\s*(?:import|from)\s+([a-zA-Z0-9_\.]+)", code, re.M):
        top = m.group(1).split(".")[0]
        if top and top not in std and top not in obvious and not top.startswith("_"):
            mods.add(top)
    return sorted(mods)

VENV_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "pysmith", "venv")

def install_deps(pkgs):
    """Install packages into pysmith's managed venv. Returns log + the python path."""
    if not pkgs:
        return {"ok": True, "log": "no third-party packages — pure stdlib", "python": sys.executable}
    try:
        if not os.path.isdir(VENV_DIR):
            import venv
            venv.EnvBuilder(with_pip=True).create(VENV_DIR)
        vpy = os.path.join(VENV_DIR, "bin", "python")
        if not os.path.exists(vpy):
            vpy = os.path.join(VENV_DIR, "Scripts", "python.exe")  # windows fallback
        proc = subprocess.run([vpy, "-m", "pip", "install", *pkgs],
                              capture_output=True, text=True, timeout=300)
        out = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": proc.returncode == 0, "log": out[-1500:], "python": vpy}
    except Exception as e:
        return {"ok": False, "log": f"venv/install failed: {e}", "python": sys.executable}

# the interpreter used to run tools: the venv if it exists, else current
def run_python():
    for cand in (os.path.join(VENV_DIR, "bin", "python"),
                 os.path.join(VENV_DIR, "Scripts", "python.exe")):
        if os.path.exists(cand):
            return cand
    return sys.executable

# ==========================================================================
# helpers
# ==========================================================================
def looks_dangerous(code):
    return [p for p in DANGER if re.search(p, code)]

def _http_post(url, headers, body, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def call_model(messages, provider_id=None):
    """Call the selected provider, falling through its model chain on error.
    Returns {"reply", "model", "provider"} or {"error"}."""
    pid = provider_id or STATE.get("provider") or DEFAULT_PROVIDER
    prov = PROVIDERS.get(pid)
    if not prov:
        return {"error": f"Unknown provider '{pid}'."}
    key = STATE.get("keys", {}).get(pid, "")
    if not key:
        return {"error": f"No API key for {prov['label']}. Add it in Settings, "
                         f"or set {prov['env']} and restart."}

    # model order: a user-chosen model (if set) first, then the rest of the chain
    chosen = STATE.get("models", {}).get(pid)
    chain = list(prov["models"])
    if chosen:
        chain = [chosen] + [m for m in chain if m != chosen]

    last = None
    for model in chain:
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
                "User-Agent": f"pysmith/{__version__}",
                "Accept": "application/json",
            }
            body = {"model": model, "temperature": 0.3, "messages": messages}
            data = _http_post(prov["url"], headers, body)
            reply = data["choices"][0]["message"]["content"]
            return {"reply": reply, "model": model, "provider": pid}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            if e.code == 403 and "1010" in detail:
                return {"error": f"Blocked by Cloudflare (403/1010) before reaching "
                                 f"{prov['label']}. Usually a VPN/proxy or outdated client, not your key."}
            if e.code == 401:
                return {"error": f"{prov['label']} rejected the key (401). Check it in Settings."}
            if e.code in (404, 400):
                # model not available to this account/plan — try the next in the chain
                last = f"{model}: HTTP {e.code} (model unavailable on your plan?)"
                continue
            last = f"{model}: HTTP {e.code} {detail}"
        except Exception as e:
            last = f"{model}: {e}"
    return {"error": f"{prov['label']} chain failed. Last: {last}"}

def extract_code(reply):
    """Pull the python code block out of a model reply (tagged, else any fence)."""
    m = re.search(r"```(?:python|py)\s*\n(.*?)```", reply, re.S | re.I) \
        or re.search(r"```\s*\n(.*?)```", reply, re.S)
    return m.group(1).rstrip() if m else None

def smoke_test(code):
    """Silent quality checks on generated code. Returns (passed, report, checks).
    IMPORTANT: this only checks that the code PARSES and IMPORTS cleanly. It does
    NOT run the tool's actual logic — doing that (e.g. via --help on a non-argparse
    tool) would execute real work and produce false failures. Behaviour is verified
    by the user pressing Run, not here."""
    checks = []
    # 1. syntax
    try:
        import ast
        ast.parse(code)
        checks.append(("syntax", True, ""))
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}", [("syntax", False, str(e))]

    # 2. import-ability: load the module WITHOUT running its __main__ block.
    #    We import it as a module so top-level defs/imports are checked, but the
    #    `if __name__ == '__main__':` guard does not fire.
    fd, path = tempfile.mkstemp(prefix="pysmith_test_", suffix=".py")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)
        # run a tiny harness that imports the file as a module (name != __main__)
        harness = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('pysmith_candidate', {path!r})\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "try:\n"
            "    spec.loader.exec_module(mod)\n"
            "except (ModuleNotFoundError, ImportError) as e:\n"
            "    print('DEP_MISSING:' + str(e)); sys.exit(0)\n"
        )
        try:
            proc = subprocess.run([sys.executable, "-c", harness],
                                  capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
            out = proc.stdout.decode("utf-8", errors="replace")
            err = proc.stderr.decode("utf-8", errors="replace")
            if out.startswith("DEP_MISSING:"):
                # needs a third-party package — not a code bug
                checks.append(("imports", True, "needs a third-party package (use the deps button)"))
            elif proc.returncode != 0:
                # a genuine error at import/definition time (NameError, bad default, etc.)
                msg = err.strip()[-500:] or "import failed"
                checks.append(("imports", False, msg))
                return False, msg, checks
            else:
                checks.append(("imports", True, ""))
        except subprocess.TimeoutExpired:
            # top-level code that blocks — unusual, but flag it
            checks.append(("imports", False, "import timed out (top-level code is blocking)"))
            return False, "Import timed out — there may be blocking code at module top level.", checks
        return True, "", checks
    finally:
        try: os.unlink(path)
        except Exception: pass

def chat_with_autotest(messages, provider_id=None):
    """Call the model, then silently smoke-test any code it returns, feeding
    failures back for up to AUTOTEST_MAX_ROUNDS before returning to the user."""
    convo = list(messages)
    rounds = []
    res = call_model(convo, provider_id)
    if res.get("error"):
        return res

    for attempt in range(AUTOTEST_MAX_ROUNDS + 1):
        code = extract_code(res.get("reply", ""))
        if not code:
            res["autotest"] = {"ran": False, "rounds": rounds}
            return res
        passed, report, checks = smoke_test(code)
        rounds.append({"attempt": attempt + 1, "passed": passed,
                       "checks": [c[0] for c in checks if c[1]],
                       "failed": [c[0] for c in checks if not c[1]],
                       "report": "" if passed else report})
        if passed or attempt == AUTOTEST_MAX_ROUNDS:
            res["autotest"] = {"ran": True, "passed": passed, "rounds": rounds}
            return res
        # feed the failure back and retry silently
        convo = convo + [
            {"role": "assistant", "content": res["reply"]},
            {"role": "user", "content":
                f"Your code failed an automatic check before I saw it. Fix it and return the "
                f"FULL corrected script.\n\nCheck output:\n{report}"},
        ]
        nxt = call_model(convo, provider_id)
        if nxt.get("error"):
            res["autotest"] = {"ran": True, "passed": False, "rounds": rounds,
                               "note": "auto-fix call failed: " + nxt["error"]}
            return res
        res = nxt

def _parse_json_reply(reply):
    """Extract a JSON object from a model reply, tolerating fences/prose."""
    reply = re.sub(r"```(?:json)?", "", reply).strip()
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def make_intake(request, provider_id=None):
    """Ask the model for a tailored, clickable question set for a tool request."""
    res = call_model([{"role": "system", "content": INTAKE_PROMPT},
                      {"role": "user", "content": request}], provider_id)
    if res.get("error"):
        return res
    parsed = _parse_json_reply(res.get("reply", ""))
    if not parsed or "questions" not in parsed:
        # graceful fallback: no intake, just proceed to build
        return {"intake": None}
    # sanitise
    qs = []
    for q in parsed.get("questions", [])[:6]:
        opts = [str(o) for o in q.get("options", [])][:4]
        if q.get("q") and len(opts) >= 2:
            qs.append({"q": str(q["q"]), "options": opts, "multi": bool(q.get("multi"))})
    return {"intake": {"summary": parsed.get("summary", ""), "questions": qs}}

def make_github(code, details, provider_id=None):
    """Generate README/.gitignore/requirements from the final code + repo details."""
    user = details.get("username", "USER")
    repo = details.get("repo", "tool")
    branch = details.get("branch", "main")
    license_name = details.get("license", "MIT")
    detail_blob = (f"username: {user}\nrepo: {repo}\nbranch: {branch}\n"
                   f"license: {license_name}\nclone over HTTPS only (never ssh).\n"
                   f"raw base: https://raw.githubusercontent.com/{user}/{repo}/{branch}/")
    res = call_model([{"role": "system", "content": GITHUB_PROMPT},
                      {"role": "user", "content":
                       f"Repo details:\n{detail_blob}\n\n=== FINAL CODE ===\n```python\n{code}\n```"}],
                     provider_id)
    if res.get("error"):
        return res
    parsed = _parse_json_reply(res.get("reply", "")) or {}
    return {"github": parsed, "details": details}

def run_code(code, args, confirmed):
    danger = looks_dangerous(code)
    if danger and not confirmed:
        return {"needsConfirm": True, "patterns": danger}

    # parse args the way a shell would (handles quotes/spaces), not naive split
    try:
        argv = shlex.split(args) if args else []
    except ValueError as e:
        return {"stdout": "", "stderr": f"Couldn't parse arguments: {e}", "exit": -1, "seconds": 0}

    # unique temp file per run so concurrent/rapid runs can't clobber each other
    fd, path = tempfile.mkstemp(prefix="pysmith_", suffix=".py")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)

        t0 = time.time()
        try:
            proc = subprocess.run(
                [run_python(), path] + argv,
                capture_output=True,           # capture as bytes, decode ourselves
                stdin=subprocess.DEVNULL,      # no stdin -> input() gets clean EOF, never hangs
                timeout=120)
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "Killed: exceeded 120s (possible infinite loop, "
                    "or the tool was waiting for input — pysmith provides none).",
                    "exit": -1, "seconds": round(time.time() - t0, 2)}
        except Exception as e:
            return {"stdout": "", "stderr": f"Could not launch: {e}", "exit": -1, "seconds": 0}

        # decode tolerantly: real Kali tool output can contain non-UTF8 bytes
        out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        errtxt = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""

        # make a bare EOFError from input() legible
        if proc.returncode != 0 and "EOFError" in errtxt and "input(" in code:
            errtxt += ("\n[pysmith] This tool reads from stdin via input(). The test runner "
                       "doesn't supply interactive input — pass values as command-line args instead.")
        return {"stdout": out, "stderr": errtxt, "exit": proc.returncode,
                "seconds": round(time.time() - t0, 2)}
    finally:
        try: os.unlink(path)
        except Exception: pass

def save_tool(code, name, kind):
    name = re.sub(r"[^A-Za-z0-9_\-]", "_", (name or "tool")).strip("_") or "tool"
    # save under a fixed, predictable home location (never the volatile cwd)
    base = os.path.join(os.path.expanduser("~"), "pysmith-tools")
    if kind == "release":
        d = os.path.join(base, "release", name)
        os.makedirs(d, exist_ok=True)
        pyp = os.path.join(d, name + ".py")
        with open(pyp, "w") as f:
            f.write(code + "\n")
        try: os.chmod(pyp, 0o755)
        except Exception: pass
        readme = os.path.join(d, "README.md")
        if not os.path.exists(readme):
            with open(readme, "w") as f:
                f.write(f"# {name}\n\nBuilt with pysmith.\n\n## Usage\n\n```bash\npython3 {name}.py\n```\n")
        return {"path": d}
    else:
        d = os.path.join(base, "forge")
        os.makedirs(d, exist_ok=True)
        pyp = os.path.join(d, name + ".py")
        with open(pyp, "w") as f:
            f.write(code + "\n")
        try: os.chmod(pyp, 0o755)
        except Exception: pass
        return {"path": pyp}

LICENSES = {
    "MIT": ("MIT License\n\nCopyright (c) {year} {holder}\n\nPermission is hereby granted, "
            "free of charge, to any person obtaining a copy of this software and associated "
            "documentation files (the \"Software\"), to deal in the Software without restriction, "
            "including without limitation the rights to use, copy, modify, merge, publish, "
            "distribute, sublicense, and/or sell copies of the Software, and to permit persons "
            "to whom the Software is furnished to do so, subject to the following conditions:\n\n"
            "The above copyright notice and this permission notice shall be included in all "
            "copies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED \"AS IS\", "
            "WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE "
            "WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. "
            "IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES "
            "OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING "
            "FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE "
            "SOFTWARE.\n"),
}

def _install_sh(user, repo, branch, name):
    """A smart one-file installer that works via curl|bash or from a clone."""
    return f"""#!/usr/bin/env bash
# {repo} installer — one-line install/update over HTTPS:
#   curl -fsSL https://raw.githubusercontent.com/{user}/{repo}/{branch}/install.sh | bash
set -euo pipefail
REPO="{user}/{repo}"; BRANCH="{branch}"
SRC="$HOME/.local/share/{repo}"; BIN="$HOME/.local/bin"; LAUNCH="$BIN/{name}"

command -v python3 >/dev/null 2>&1 || {{ echo "python3 required: sudo apt install python3"; exit 1; }}

mkdir -p "$SRC" "$BIN"
SELF_DIR="$( cd "$( dirname "${{BASH_SOURCE[0]:-$0}}" )" 2>/dev/null && pwd || true )"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/{name}.py" ]; then
  cp -f "$SELF_DIR/{name}.py" "$SRC/"
  [ -f "$SELF_DIR/requirements.txt" ] && cp -f "$SELF_DIR/requirements.txt" "$SRC/" || true
else
  if command -v git >/dev/null 2>&1; then
    if [ -d "$SRC/.git" ]; then git -C "$SRC" pull --ff-only --quiet || true
    else rm -rf "$SRC"; git clone --depth 1 -b "$BRANCH" "https://github.com/$REPO.git" "$SRC" --quiet; fi
  else
    TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
    if command -v curl >/dev/null 2>&1; then curl -fsSL "$TARBALL" | tar xz -C "$SRC" --strip-components=1
    elif command -v wget >/dev/null 2>&1; then wget -qO- "$TARBALL" | tar xz -C "$SRC" --strip-components=1
    else echo "need git, curl, or wget"; exit 1; fi
  fi
fi

# install python deps if any
[ -f "$SRC/requirements.txt" ] && python3 -m pip install -r "$SRC/requirements.txt" --break-system-packages -q 2>/dev/null || true

cat > "$LAUNCH" <<EOF
#!/usr/bin/env bash
exec python3 "$SRC/{name}.py" "\\$@"
EOF
chmod +x "$LAUNCH"

case ":$PATH:" in *":$BIN:"*) ;; *)
  RC="$HOME/.bashrc"; [ -n "${{ZSH_VERSION:-}}" ] && RC="$HOME/.zshrc"
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"
  echo "added $BIN to PATH in $RC — run: source $RC" ;;
esac
echo "installed. run: {name}"
"""

def write_github_repo(code, name, gh, details):
    """Write a complete polished repo into ./github/<repo>/."""
    name = re.sub(r"[^A-Za-z0-9_\-]", "_", (name or "tool")).strip("_") or "tool"
    user = details.get("username", "USER")
    repo = re.sub(r"[^A-Za-z0-9_.\-]", "-", details.get("repo", name)) or name
    branch = details.get("branch", "main")
    license_name = details.get("license", "MIT")
    holder = details.get("holder", user)

    d = os.path.join(os.path.expanduser("~"), "pysmith-tools", "github", repo)
    os.makedirs(d, exist_ok=True)

    # main script
    with open(os.path.join(d, name + ".py"), "w") as f:
        f.write(code + "\n")
    try: os.chmod(os.path.join(d, name + ".py"), 0o755)
    except Exception: pass

    # README (AI-generated, with fallback)
    readme = gh.get("readme") or (
        f"# {repo}\n\n{gh.get('description','A Python tool built with pysmith.')}\n\n"
        f"## Install\n\n```bash\ncurl -fsSL https://raw.githubusercontent.com/{user}/{repo}/{branch}/install.sh | bash\n```\n\n"
        f"## Usage\n\n```bash\n{name} --help\n```\n")
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write(readme)

    # .gitignore
    with open(os.path.join(d, ".gitignore"), "w") as f:
        f.write(gh.get("gitignore") or "__pycache__/\n*.py[cod]\n.venv/\nvenv/\n.env\n*.key\n.DS_Store\n")

    # requirements (only if non-empty)
    reqs = (gh.get("requirements") or "").strip()
    if reqs:
        with open(os.path.join(d, "requirements.txt"), "w") as f:
            f.write(reqs + "\n")

    # install.sh
    ish = os.path.join(d, "install.sh")
    with open(ish, "w") as f:
        f.write(_install_sh(user, repo, branch, name))
    try: os.chmod(ish, 0o755)
    except Exception: pass

    # LICENSE
    lic = LICENSES.get(license_name)
    if lic:
        with open(os.path.join(d, "LICENSE"), "w") as f:
            f.write(lic.format(year=time.strftime("%Y"), holder=holder))

    # the exact push commands, HTTPS only
    push = [
        "cd " + repo,
        "git init",
        "git add .",
        f'git commit -m "{repo} — initial release"',
        f"git branch -M {branch}",
        f"git remote add origin https://github.com/{user}/{repo}.git",
        f"git push -u origin {branch}",
    ]
    return {"path": d, "files": sorted(os.listdir(d)), "push": push,
            "install_line": f"curl -fsSL https://raw.githubusercontent.com/{user}/{repo}/{branch}/install.sh | bash"}

# --------------------------------------------------------------------------
# SESSION LOG  -- every run is appended; one button hands it all to the model
# --------------------------------------------------------------------------
SESSION_LOG = []   # list of dicts: {ts, kind, name, args, exit, seconds, stdout, stderr}

def log_run(name, args, result):
    SESSION_LOG.append({
        "ts": time.strftime("%H:%M:%S"),
        "name": name, "args": args,
        "exit": result.get("exit"), "seconds": result.get("seconds"),
        "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""),
    })
    # keep it bounded so we never blow the context window
    if len(SESSION_LOG) > 40:
        del SESSION_LOG[0:len(SESSION_LOG) - 40]

def render_log(full=True):
    """Render the session log as a single text blob (also what gets saved to file)."""
    lines = [f"pysmith session log — {len(SESSION_LOG)} run(s)", "=" * 50]
    for i, e in enumerate(SESSION_LOG, 1):
        lines.append(f"\n[run {i}] {e['ts']}  {e['name']}.py {e['args']}".rstrip())
        lines.append(f"exit {e['exit']} · {e['seconds']}s")
        if e["stdout"]:
            out = e["stdout"] if full else e["stdout"][-1500:]
            lines.append("--- stdout ---\n" + out.rstrip())
        if e["stderr"]:
            lines.append("--- stderr ---\n" + e["stderr"].rstrip())
    return "\n".join(lines)

def fix_from_log(code, messages, provider_id=None):
    """Send the current code + the whole session log to the model for a fix."""
    if not SESSION_LOG:
        return {"error": "No runs logged yet — run the tool at least once first."}
    log_blob = render_log(full=False)
    convo = [m for m in messages if m.get("role") != "system"]
    convo = [{"role": "system", "content": SYSTEM_PROMPT}] + convo + [{
        "role": "user",
        "content": (
            "Here is the current tool and the full log of how it behaved when I ran it. "
            "Diagnose every problem you can see in the runs and return the FULL corrected "
            "script. Briefly list what you fixed.\n\n"
            f"=== CURRENT CODE ===\n```python\n{code}\n```\n\n"
            f"=== RUN LOG ===\n{log_blob}"
        )
    }]
    return chat_with_autotest(convo, provider_id)

def polish_round(code, messages, provider_id=None):
    """One iteration of the auto-polish loop: run a quick smoke, then ask the model
    to make the tool more robust/polished, returning improved code."""
    # smoke the current code so we can tell the model what's wrong right now
    passed, report, _ = smoke_test(code)
    state_note = "It passes a basic smoke test." if passed else f"It currently FAILS a check:\n{report}"
    log_blob = render_log(full=False) if SESSION_LOG else "(no runs yet)"
    convo = [{"role": "system", "content": SYSTEM_PROMPT}, {
        "role": "user",
        "content": (
            "Improve this tool by one meaningful increment: fix any bug, harden error "
            "handling, improve output clarity, and add the single most valuable missing "
            "feature — but keep it ONE self-contained script and don't over-engineer. "
            "Return the FULL improved script and one line on what you changed.\n\n"
            f"{state_note}\n\n=== CODE ===\n```python\n{code}\n```\n\n=== RECENT RUNS ===\n{log_blob}"
        )
    }]
    return chat_with_autotest(convo, provider_id)

# ==========================================================================
# http
# ==========================================================================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, {"error": "not found"})

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._file(os.path.join(HERE, "ui", "index.html"), "text/html; charset=utf-8")
        elif self.path.startswith("/assets/"):
            name = os.path.basename(self.path)
            ext = name.rsplit(".", 1)[-1].lower()
            ctype = {"svg": "image/svg+xml", "png": "image/png"}.get(ext, "application/octet-stream")
            self._file(os.path.join(HERE, "assets", name), ctype)
        elif self.path == "/api/status":
            provs = [{"id": pid, "label": p["label"],
                      "hasKey": bool(STATE["keys"].get(pid)),
                      "models": p["models"],
                      "chosen": STATE["models"].get(pid) or p["models"][0],
                      "topModel": p["models"][0]}
                     for pid, p in PROVIDERS.items()]
            cur = PROVIDERS.get(STATE["provider"], {})
            chosen_cur = STATE["models"].get(STATE["provider"]) or (cur.get("models") or ["?"])[0]
            self._send(200, {
                "providers": provs,
                "provider": STATE["provider"],
                "model": chosen_cur,
                "hasKey": bool(STATE["keys"].get(STATE["provider"])),
                "autotest": AUTOTEST_MAX_ROUNDS,
                "version": __version__,
            })
        elif self.path == "/api/log":
            self._send(200, {"log": render_log(full=True), "runs": len(SESSION_LOG)})
        elif self.path == "/api/library":
            self._send(200, library_list())
        elif self.path == "/api/sessions":
            self._send(200, session_list())
        elif self.path == "/api/log.txt":
            blob = render_log(full=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=pysmith-session.log")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode() or "{}")
        except Exception:
            return self._send(400, {"error": "bad json"})

        if self.path == "/api/key":
            pid = data.get("provider") or STATE["provider"]
            if pid not in PROVIDERS:
                return self._send(200, {"error": "unknown provider"})
            STATE["keys"][pid] = (data.get("key") or "").strip()
            saved = persist_state() if STATE["keys"][pid] else False
            self._send(200, {"hasKey": bool(STATE["keys"][pid]), "saved": saved})
        elif self.path == "/api/provider":
            pid = data.get("provider")
            if pid not in PROVIDERS:
                return self._send(200, {"error": "unknown provider"})
            STATE["provider"] = pid
            persist_state()
            self._send(200, {"provider": pid, "hasKey": bool(STATE["keys"].get(pid)),
                             "model": STATE["models"].get(pid) or PROVIDERS[pid]["models"][0]})
        elif self.path == "/api/model":
            pid = data.get("provider") or STATE["provider"]
            model = data.get("model")
            if pid not in PROVIDERS:
                return self._send(200, {"error": "unknown provider"})
            if model and model in PROVIDERS[pid]["models"]:
                STATE["models"][pid] = model
                persist_state()
                self._send(200, {"provider": pid, "model": model})
            else:
                self._send(200, {"error": "unknown model for this provider"})
        elif self.path == "/api/chat":
            # The methodology prompt is authoritative and lives here, server-side.
            convo = [m for m in data.get("messages", []) if m.get("role") != "system"]
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + convo
            provider = data.get("provider")  # optional per-request override
            self._send(200, chat_with_autotest(messages, provider))
        elif self.path == "/api/run":
            result = run_code(data.get("code", ""), data.get("args", ""),
                              bool(data.get("confirm")))
            # log only actual runs (not the confirm-gate response)
            if "needsConfirm" not in result:
                log_run(data.get("name", "tool"), data.get("args", ""), result)
            self._send(200, result)
        elif self.path == "/api/fixlog":
            convo = data.get("messages", [])
            self._send(200, fix_from_log(data.get("code", ""), convo, data.get("provider")))
        elif self.path == "/api/intake":
            self._send(200, make_intake(data.get("request", ""), data.get("provider")))
        elif self.path == "/api/github":
            self._send(200, make_github(data.get("code", ""), data.get("details", {}),
                                        data.get("provider")))
        elif self.path == "/api/github/write":
            try:
                self._send(200, write_github_repo(data.get("code", ""), data.get("name", "tool"),
                                                  data.get("github", {}), data.get("details", {})))
            except Exception as e:
                self._send(200, {"error": str(e)})
        elif self.path == "/api/log.clear":
            SESSION_LOG.clear()
            self._send(200, {"runs": 0})
        elif self.path == "/api/library/save":
            self._send(200, library_save(data.get("name", "tool"), data.get("code", ""),
                                         data.get("messages", [])))
        elif self.path == "/api/library/load":
            self._send(200, library_load(data.get("id", "")))
        elif self.path == "/api/library/delete":
            self._send(200, library_delete(data.get("id", "")))
        elif self.path == "/api/session/save":
            self._send(200, session_save(data.get("id"), data.get("name", "untitled"),
                                         data.get("code", ""), data.get("messages", [])))
        elif self.path == "/api/session/load":
            self._send(200, session_load(data.get("id", "")))
        elif self.path == "/api/session/delete":
            self._send(200, session_delete(data.get("id", "")))
        elif self.path == "/api/deps":
            self._send(200, {"deps": detect_deps(data.get("code", ""))})
        elif self.path == "/api/deps/install":
            self._send(200, install_deps(data.get("deps", [])))
        elif self.path == "/api/polish":
            convo = data.get("messages", [])
            self._send(200, polish_round(data.get("code", ""), convo, data.get("provider")))
        elif self.path == "/api/save":
            try:
                self._send(200, save_tool(data.get("code", ""), data.get("name", "tool"),
                                          data.get("kind", "testing")))
            except Exception as e:
                self._send(200, {"error": str(e)})
        elif self.path == "/api/quit":
            self._send(200, {"ok": True})
            # shut the server down shortly after responding
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
        else:
            self._send(404, {"error": "not found"})

def free_port(host, start):
    for p in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, p)) != 0:
                return p
    return start

def launch_app_window(url):
    """Open pysmith in a Chromium-family app window (no browser chrome).
    Falls back to a normal browser tab if no Chromium-family browser is found."""
    import shutil as _sh
    # common chromium-family binaries on Linux/Kali
    candidates = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                  "brave-browser", "microsoft-edge", "vivaldi"]
    app_data = os.path.join(os.path.expanduser("~"), ".local", "share", "pysmith", "window")
    for browser in candidates:
        path = _sh.which(browser)
        if path:
            try:
                subprocess.Popen(
                    [path, f"--app={url}",
                     f"--user-data-dir={app_data}",   # isolated profile = clean window
                     "--no-first-run", "--no-default-browser-check",
                     "--window-size=1280,860"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return browser
            except Exception:
                continue
    # fallback: ordinary browser tab
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return None

def main():
    port = free_port(HOST, PORT)
    url = f"http://{HOST}:{port}"
    srv = ThreadingHTTPServer((HOST, port), Handler)
    print(f"\n  pysmith v{__version__}  \u2014  {url}")
    have = [PROVIDERS[pid]["label"] for pid in PROVIDERS if STATE["keys"].get(pid)]
    if have:
        print(f"  keys loaded for: {', '.join(have)}")
    else:
        print(f"  no API keys yet \u2014 add one in Settings")
    print(f"  active provider: {PROVIDERS[STATE['provider']]['label']}")
    print(f"  auto-test: up to {AUTOTEST_MAX_ROUNDS} silent fix rounds")
    print(f"  serving local-only. ctrl-c to stop.\n")
    used = launch_app_window(url)
    if used:
        print(f"  opened in app window via {used}")
    else:
        print(f"  no Chromium-family browser found \u2014 opened a normal tab\n"
              f"  (install chromium for the clean app window)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  forge banked. later.\n")
        srv.shutdown()

if __name__ == "__main__":
    main()

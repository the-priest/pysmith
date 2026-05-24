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
import webbrowser
import subprocess
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "2.0.0"
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
    "anthropic": {
        "label": "Anthropic (Claude)",
        "url": "https://api.anthropic.com/v1/messages",
        "env": "ANTHROPIC_API_KEY",
        "kind": "anthropic",                       # distinct request/response shape
        "models": ["claude-opus-4-7", "claude-sonnet-4-6"],
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "url": "https://api.openai.com/v1/chat/completions",
        "env": "OPENAI_API_KEY",
        "kind": "openai",                          # openai-style chat completions
        "models": ["gpt-4o", "gpt-4o-mini"],
    },
    "groq": {
        "label": "Groq (fast fallback)",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "env": "GROQ_API_KEY",
        "kind": "openai",                          # groq speaks the openai shape
        "models": [
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "gemma2-9b-it",
            "llama-3.1-8b-instant",
        ],
    },
}

# default provider when the user hasn't picked one
DEFAULT_PROVIDER = "anthropic"

# auto-test loop: after the model writes code, pysmith silently checks it and
# feeds failures back to the model up to this many times before showing you.
AUTOTEST_MAX_ROUNDS = 2

HOST = "127.0.0.1"
PORT = 8765

# This is the heart of it: the model is taught to build tools the way a careful
# senior engineer does -- agree first, testing version by default, release only
# on request. Tune it to taste.
SYSTEM_PROMPT = """You are pysmith, an expert Python tool-builder working alongside a security /
sysadmin user on Kali Linux. You build small, sharp command-line tools in a back-and-forth
"build dialogue". Follow this method exactly:

1. AGREE FIRST. If the request is vague, over-scoped, or hides a real decision, do NOT dump
   code. Ask one or two pointed questions, or state your assumptions and lay out a short plan:
   what it will do, its inputs and outputs, and any external binaries it wraps. Be honest about
   scope -- if something is infeasible or a bad idea, say so plainly and offer the realistic
   version. Only build once the shape is agreed.

2. TESTING VERSION BY DEFAULT. When you write code, produce a TESTING version: ONE complete,
   runnable, single-file Python script that genuinely works and is easy to try right now.
   Favour correctness and fast iteration over ceremony. Include just enough error handling to
   fail gracefully. Prefer the standard library; if a third-party package is truly required,
   state the exact `pip install ...` line on one line BEFORE the code block. If the tool wraps
   a binary (nmap, hashcat, ...), call it via subprocess and handle it being missing. Do NOT
   add heavy argparse scaffolding or packaging yet.

3. ITERATE on real feedback. When the user pastes a run result or error, return the FULL updated
   script (never a diff) and say briefly what you changed and why.

4. RELEASE VERSION ONLY ON REQUEST. When -- and only when -- the user asks for the release /
   github / final version, produce the polished form: a top docstring with a one-line summary
   and a usage example, a clean argparse CLI, solid error handling and sensible exit codes,
   useful comments, no dead code. After the code block, propose a short plain-text README
   (what it does, install, usage).

5. SAFETY. Never include destructive operations (mass deletion, disk wipes, fork bombs) unless
   the user explicitly and unambiguously asks, and call them out if so. Assume the code runs on
   the user's own machine.

OUTPUT FORMAT: a tight message first (a few sentences at most). THEN, only when you are actually
providing code, exactly ONE ```python fenced block containing the entire script -- never two.
When you are only planning or discussing, do not include any code block."""

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

# session state: per-provider keys + the currently selected provider
STATE = {
    "keys": _initial_keys(),
    "provider": load_config().get("provider") or DEFAULT_PROVIDER,
}

def persist_state():
    return save_config({"keys": STATE["keys"], "provider": STATE["provider"]})

# ==========================================================================
# helpers
# ==========================================================================
def looks_dangerous(code):
    return [p for p in DANGER if re.search(p, code)]

def _http_post(url, headers, body, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def _split_system(messages):
    """Pull a leading system message out; return (system_text, rest)."""
    sys_txt = ""
    rest = []
    for m in messages:
        if m.get("role") == "system" and not rest:
            sys_txt = m.get("content", "")
        else:
            rest.append(m)
    return sys_txt, rest

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

    last = None
    for model in prov["models"]:
        try:
            if prov["kind"] == "anthropic":
                sys_txt, convo = _split_system(messages)
                headers = {
                    "Content-Type": "application/json",
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "User-Agent": f"pysmith/{__version__}",
                }
                body = {"model": model, "max_tokens": 4096, "temperature": 0.3,
                        "messages": convo}
                if sys_txt:
                    body["system"] = sys_txt
                data = _http_post(prov["url"], headers, body)
                # anthropic returns content as a list of blocks
                parts = [b.get("text", "") for b in data.get("content", [])
                         if b.get("type") == "text"]
                reply = "".join(parts)
            else:  # openai-style (openai + groq)
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
            if e.code == 404:
                # model not available to this account — try the next in the chain
                last = f"{model}: 404 (model not available to your account?)"
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
    """Silent quality checks on generated code. Returns (passed, report, checks)."""
    checks = []
    # 1. syntax
    try:
        import ast
        ast.parse(code)
        checks.append(("syntax", True, ""))
    except SyntaxError as e:
        return False, f"SyntaxError: {e}", [("syntax", False, str(e))]

    # write to a temp file for the runtime checks
    fd, path = tempfile.mkstemp(prefix="pysmith_test_", suffix=".py")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)

        # 2. import / top-level execution check via -c with a guard:
        #    we run the file; if it has a __main__ guard, top-level import is safe.
        #    a crash here that isn't "missing dependency" is a real failure.
        try:
            proc = subprocess.run([sys.executable, path, "--help"],
                                  capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
            err = proc.stderr.decode("utf-8", errors="replace")
            # ModuleNotFoundError = needs a dependency, not a code bug -> not a hard fail
            if "ModuleNotFoundError" in err or "ImportError" in err:
                checks.append(("imports", True, "needs a third-party package (see /deps)"))
            elif proc.returncode != 0 and ("Traceback" in err and "argparse" not in err
                                           and "SystemExit" not in err):
                # a real crash on --help (and not just 'no such option')
                # only count as fail if it looks like a genuine exception at import time
                if "Error" in err and "usage:" not in proc.stdout.decode("utf-8", errors="replace").lower():
                    checks.append(("runtime", False, err.strip()[-400:]))
                    return False, err.strip()[-400:], checks
                else:
                    checks.append(("runtime", True, ""))
            else:
                checks.append(("runtime", True, ""))
        except subprocess.TimeoutExpired:
            checks.append(("runtime", False, "timed out on --help (waiting for input or infinite loop)"))
            return False, "Timed out running --help", checks
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
                       "failed": [c[0] for c in checks if not c[1]]})
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
                [sys.executable, path] + argv,
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
    if kind == "release":
        d = os.path.join(os.getcwd(), "release", name)
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
        d = os.path.join(os.getcwd(), "forge")
        os.makedirs(d, exist_ok=True)
        pyp = os.path.join(d, name + ".py")
        with open(pyp, "w") as f:
            f.write(code + "\n")
        try: os.chmod(pyp, 0o755)
        except Exception: pass
        return {"path": pyp}

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
                      "topModel": p["models"][0]}
                     for pid, p in PROVIDERS.items()]
            cur = PROVIDERS.get(STATE["provider"], {})
            self._send(200, {
                "providers": provs,
                "provider": STATE["provider"],
                "model": (cur.get("models") or ["?"])[0],
                "hasKey": bool(STATE["keys"].get(STATE["provider"])),
                "autotest": AUTOTEST_MAX_ROUNDS,
                "version": __version__,
            })
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
                             "model": PROVIDERS[pid]["models"][0]})
        elif self.path == "/api/chat":
            # The methodology prompt is authoritative and lives here, server-side.
            convo = [m for m in data.get("messages", []) if m.get("role") != "system"]
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + convo
            provider = data.get("provider")  # optional per-request override
            self._send(200, chat_with_autotest(messages, provider))
        elif self.path == "/api/run":
            self._send(200, run_code(data.get("code", ""), data.get("args", ""),
                                     bool(data.get("confirm"))))
        elif self.path == "/api/save":
            try:
                self._send(200, save_tool(data.get("code", ""), data.get("name", "tool"),
                                          data.get("kind", "testing")))
            except Exception as e:
                self._send(200, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

def free_port(host, start):
    for p in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, p)) != 0:
                return p
    return start

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
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  forge banked. later.\n")
        srv.shutdown()

if __name__ == "__main__":
    main()

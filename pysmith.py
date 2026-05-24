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

# Groq fallback chain, biggest -> smallest. Tried in order, falls through on
# error / rate-limit.  >>> Paste your own verified model strings here. <<<
MODELS = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
]

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
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

# session state (in memory only)
STATE = {"key": os.environ.get("GROQ_API_KEY", "").strip()}

# ==========================================================================
# helpers
# ==========================================================================
def looks_dangerous(code):
    return [p for p in DANGER if re.search(p, code)]

def call_groq(messages):
    key = STATE.get("key", "")
    if not key:
        return {"error": "No Groq API key set. Add it in Settings, or set GROQ_API_KEY and restart."}
    last = None
    for model in MODELS:
        body = json.dumps({"model": model, "temperature": 0.3,
                           "messages": messages}).encode()
        req = urllib.request.Request(
            GROQ_URL, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key,
                     # Cloudflare in front of the API rejects the default
                     # Python-urllib User-Agent with 403 (code 1010), so we
                     # present as a normal client.
                     "User-Agent": f"pysmith/{__version__}",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            return {"reply": data["choices"][0]["message"]["content"], "model": model}
        except urllib.error.HTTPError as e:
            last = f"{model}: HTTP {e.code} {e.read().decode(errors='replace')[:160]}"
        except Exception as e:
            last = f"{model}: {e}"
    return {"error": "Model chain failed. Last: " + str(last)}

def run_code(code, args, confirmed):
    danger = looks_dangerous(code)
    if danger and not confirmed:
        return {"needsConfirm": True, "patterns": danger}
    path = os.path.join(tempfile.gettempdir(), "pysmith_run.py")
    with open(path, "w") as f:
        f.write(code)
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, path] + (args.split() if args else []),
            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Killed: exceeded 120s.", "exit": -1,
                "seconds": round(time.time() - t0, 2)}
    except Exception as e:
        return {"stdout": "", "stderr": f"Could not launch: {e}", "exit": -1,
                "seconds": 0}
    return {"stdout": proc.stdout, "stderr": proc.stderr, "exit": proc.returncode,
            "seconds": round(time.time() - t0, 2)}

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
            self._send(200, {"hasKey": bool(STATE.get("key")), "model": MODELS[0],
                             "models": MODELS, "version": __version__})
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
            STATE["key"] = (data.get("key") or "").strip()
            self._send(200, {"hasKey": bool(STATE["key"])})
        elif self.path == "/api/chat":
            # The methodology prompt is authoritative and lives here, server-side.
            # We drop any system message the client sent and prepend our own.
            convo = [m for m in data.get("messages", []) if m.get("role") != "system"]
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + convo
            self._send(200, call_groq(messages))
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
    print(f"  Groq key: {'loaded from env' if STATE['key'] else 'not set (add it in Settings)'}")
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

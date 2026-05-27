#!/usr/bin/env python3
"""
pysmith  -  AI-assisted Python GUI toolsmith (desktop app)
==========================================================
A local workspace for building real, graphical Python tools by talking to a model.
Every tool it builds is a windowed GUI app tuned to run on Kali Linux under BOTH
KDE Plasma (desktop) and Phosh (mobile / NetHunter Pro) — adaptive, touch-friendly,
single-file. You agree on the tool in a build dialogue, it writes a TESTING version
you launch right here, you iterate, and only when you ask does it package a RELEASE
version (with a .desktop launcher so it lands in your app grid).

This file is a tiny local server (standard library only). It:
  - serves the workspace UI to your browser
  - keeps your API key on THIS machine (never sent to the browser)
  - actually LAUNCHES the generated GUI locally so "test it" is real
  - is GUI-aware: it detects the toolkit, runs with the right interpreter, surfaces
    startup errors, and never blocks waiting for a window you left open
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

__version__ = "9.0.0"
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
# The "models" lists below are only FALLBACKS. pysmith fetches each provider's
# live catalog from its OpenAI-compatible /models endpoint ("models_url") using
# your key, so the dropdown shows exactly what your account can actually call —
# no more guessing at names that 404 with "model unavailable on your plan".
PROVIDERS = {
    "groq": {
        "label": "Groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models_url": "https://api.groq.com/openai/v1/models",
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
        # SiliconFlow runs TWO separate platforms whose keys are NOT interchangeable:
        #   - International: cloud.siliconflow.COM  -> api.siliconflow.com
        #   - China:         cloud.siliconflow.CN   -> api.siliconflow.cn
        # A key made on one returns 401 on the other. We target .com because that's
        # where cloud.siliconflow.com keys are issued. If your key is from the .cn
        # site instead, change both URLs below back to .cn.
        "url": "https://api.siliconflow.com/v1/chat/completions",
        "models_url": "https://api.siliconflow.com/v1/models?sub_type=chat",
        "env": "SILICONFLOW_API_KEY",
        "kind": "openai",
        # biggest / strongest first (fallback only — live fetch overrides)
        "models": [
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen2.5-72B-Instruct",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "deepseek-ai/DeepSeek-V2.5",
            "Qwen/Qwen2.5-7B-Instruct",
        ],
    },
    "google": {
        "label": "Google AI Studio",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/openai/models",
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
        "models_url": "https://api.novita.ai/v3/openai/models",
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

# This is the heart of it: the model is taught to build GRAPHICAL tools the way a
# careful senior engineer does -- agree first, testing version by default, release
# only on request. It targets Kali Linux under both KDE Plasma and Phosh. Tune to taste.
SYSTEM_PROMPT = """You are pysmith, a senior Python engineer who builds small, sharp, genuinely
working GRAPHICAL (GUI) desktop tools for a security / sysadmin user on Kali Linux. Every tool you
produce opens a real window — never a bare command-line script. The user runs the same tool on a
KDE Plasma desktop AND on a Phosh phone (Kali NetHunter Pro), so it must look right and be usable
on both a large screen and a narrow ~360px touch screen. You write the kind of code a careful
professional ships: correct, defensive, readable, responsive. Hold yourself to that bar regardless
of how the request is phrased.

TOOLKIT (pick ONE, default to GTK 3 via PyGObject unless the user/intake says otherwise):
- DEFAULT — GTK 3 with PyGObject (`gi`). It is the native Phosh stack, ships on Kali, is
  touch-friendly, and runs fine under KDE. Use this unless told otherwise.
- PyQt5 / PySide6 — only if the user asks or the tool is desktop-only / KDE-centric.
- Tkinter — only for the very simplest tools, or when the user wants zero system dependencies.
Whatever you choose, stay on ONE toolkit for the whole tool. Never mix toolkits.

GUI ENGINEERING STANDARDS (apply to every tool you write):
- It must actually open a window and do the agreed job when launched. Mentally trace startup,
  the main interaction, and the obvious failure paths before you output.
- ADAPTIVE LAYOUT IS MANDATORY. Assume the window may be only ~360px wide on Phosh. Use
  expanding/scrolling containers (e.g. Gtk.ScrolledWindow, Gtk.Box with expand, Gtk.Grid that
  reflows), never fixed pixel sizes that overflow a phone. Set a sane default size
  (e.g. 480x640) and a small minimum; let content scroll rather than clip. Big, tap-sized
  controls (≥40px tall). Don't assume a mouse.
- NEVER FREEZE THE UI. Any work that blocks — running nmap/tcpdump/hashcat, network calls,
  scanning many hosts, reading large files — MUST run off the main thread (threading.Thread)
  and marshal results back to the GUI thread safely (GLib.idle_add for GTK; signals for Qt;
  widget.after for Tkinter). The window must stay responsive with a visible busy/progress state.
- Wrap external Kali binaries (nmap, hashcat, tcpdump, aircrack-ng, etc.) with subprocess.
  Detect when the binary is absent (shutil.which) and show a clear in-window error/dialog telling
  the user exactly what to install — never a silent failure or a raw traceback dialog.
- DEGRADE GRACEFULLY IF THE TOOLKIT IS MISSING. At the very top, import the toolkit inside a
  try/except. On failure print to stderr the exact install command and exit non-zero, e.g.:
      sudo apt install python3-gi gir1.2-gtk-3.0      (GTK 3)
      sudo apt install python3-pyqt5                  (PyQt5)
      sudo apt install python3-tk                     (Tkinter)
  For GTK also guard the version: `gi.require_version("Gtk", "3.0")` inside that try/except.
- IMPORT-SAFE STRUCTURE (so pysmith can pre-check your code without opening a window): do ALL
  widget construction inside a class and/or a main() function, and only build+run it under
  `if __name__ == "__main__":`. Top-level code must be imports and definitions only — nothing
  that opens a window, connects to a display, or blocks at import time.
- Validate inputs in the UI: check fields, file existence, ranges, formats before acting; show
  the problem inline (entry styling, a label, or a dialog), don't crash.
- No silent failure, no bare `except: pass`, no placeholder/stub callbacks presented as working.
  No invented toolkit APIs — if unsure a method exists, use a standard approach you are sure of.
- Prefer the standard library for logic. The toolkit (PyGObject/PyQt/etc.) is the only expected
  third-party dependency; if you genuinely need another package, put the exact install line on
  ONE line BEFORE the code block (apt for system/GUI libs, pip for pure-Python libs).

METHOD (the build dialogue):
1. CLARIFY BEFORE BUILDING. If meaningful details are unresolved, do not dump code — surface the
   decisions. (pysmith may run a structured intake for you; honour every answer precisely,
   including the chosen toolkit and whether it must fit a phone screen.) Once the shape is clear, build.
2. TESTING VERSION BY DEFAULT: ONE complete, runnable, single-file GUI script. Lean but correct —
   real widgets, real behaviour, full input validation, threaded work, graceful errors — but no
   packaging ceremony yet.
3. ITERATE on real feedback: when given a run result/error/log, return the FULL updated script
   (never a diff) and state briefly what you changed and why.
4. RELEASE VERSION ONLY WHEN ASKED: top docstring with summary + how to launch, clean class
   structure, an optional minimal argparse for launch flags (e.g. --version) that does NOT replace
   the GUI, robust error handling, helpful comments, zero dead code. Still a GUI app.
5. SAFETY: no destructive operations (mass deletion, disk wipes, fork bombs) unless the user
   explicitly and unambiguously asks; if so, call it out. Assume it runs on the user's own machine.

OUTPUT FORMAT: a tight message first (a few sentences). THEN, only when actually providing code,
exactly ONE ```python fenced block with the entire single-file GUI script — never two blocks. When
only planning or discussing, include no code block at all."""

# Used to generate a tailored, clickable intake for a new tool request.
INTAKE_PROMPT = """You are the requirements analyst for pysmith, a builder of GRAPHICAL (GUI) Python
tools that must run on Kali Linux under both KDE Plasma (desktop) and Phosh (phone). The user wants
to build a tool. Produce the SHORT, HIGH-VALUE set of questions needed to build EXACTLY the right
GUI — no lazy or generic filler.

Return ONLY a JSON object, no prose, no markdown fences:
{"summary": "<one line restating the GUI tool they want to build>",
 "questions": [
   {"q": "<clear question>", "options": ["<opt1>", "<opt2>", "<opt3>"], "multi": false},
   ...
 ]}

Rules:
- 3 to 6 questions MAX. Only ask what genuinely changes the code.
- ALWAYS include a toolkit question with options like ["GTK 3 (works on KDE + Phosh)",
  "PyQt5 (desktop/KDE)", "Tkinter (zero deps)"] — default-lead with GTK 3.
- ALWAYS include a target-screen question: ["Phone + desktop (adaptive)", "Desktop only",
  "Phone only"].
- Tailor the rest to THIS tool: what the main window shows (e.g. table of results, live log,
  form + output pane), what inputs the user gives (fields, file picker, target/range), whether it
  wraps an external binary (nmap/hashcat/tcpdump/etc.) or is pure-Python, and how results are
  presented/exported (in-window list, save to file, copy).
- 2 to 4 options per question. Options must be concrete and mutually distinct. Set "multi": true
  only when picking several genuinely makes sense.
- Prefer options the user can just tap. Keep them short."""

# Used by the GitHub-ready flow to assemble repo files from the user's answers.
GITHUB_PROMPT = """You are preparing a polished GitHub release of a Python GUI tool (it opens a
window; it runs on Kali under KDE and Phosh). You will be given the final code and the user's repo
details. Produce a complete, professional repo.

Return ONLY a JSON object, no prose, no markdown fences:
{"readme": "<full README.md markdown>",
 "gitignore": "<.gitignore contents>",
 "requirements": "<requirements.txt for pip-only deps, or empty string if none>",
 "apt": "<space-separated apt packages the GUI needs, e.g. 'python3-gi gir1.2-gtk-3.0', or empty>",
 "description": "<one-line repo description>"}

README requirements:
- Title, one-line description, then a short paragraph on what the GUI does and that it is adaptive
  (works on KDE desktop and Phosh phone).
- A "Requirements" section listing the system packages (the apt line) AND noting it appears in the
  app menu after install.
- An "Install" section with a ONE-LINE curl command that downloads and runs install.sh from the
  user's repo over HTTPS (never ssh). Use the raw.githubusercontent.com URL for their repo/branch.
  The same line should work for updates (re-running it).
- A "Usage" section: how to launch it (from the app grid or by name), with a sentence on the main
  window. Keep it real and copy-pasteable.
- The license name. Clean, scannable, professional. No fluff.

For "apt": detect the toolkit from the code. gi/PyGObject GTK3 -> "python3-gi gir1.2-gtk-3.0";
GTK4 -> "python3-gi gir1.2-gtk-4.0"; libadwaita -> add "gir1.2-adw-1"; PyQt5 -> "python3-pyqt5";
PySide6 -> "python3-pyside6"; tkinter -> "python3-tk". Empty string only if pure stdlib with no GUI."""

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
# LIVE MODEL CATALOG  -- ask each provider what YOUR key can actually call
# --------------------------------------------------------------------------
# Cache of {provider_id: [model_id, ...]} fetched from each provider's /models
# endpoint. Avoids the whole class of "model unavailable on your plan" errors that
# come from hardcoded names drifting out of date.
_MODEL_CACHE = {}

# Some providers run multiple regional API hosts whose keys are NOT interchangeable
# (a key from one returns 401 on the other). SiliconFlow is the prime example:
# .com (international) vs .cn (China). We try the configured host first, then the
# alternates, and REMEMBER whichever host accepted the key so every later call uses
# it. This makes "which site was my key from?" a non-issue for the user.
HOST_ALIASES = {
    "siliconflow": ["api.siliconflow.com", "api.siliconflow.cn"],
}
# {provider_id: working_host} once discovered for the current key
_HOST_OK = {}

def _provider_urls(provider_id):
    """Yield (chat_url, models_url) candidates for a provider, best-known host first."""
    prov = PROVIDERS[provider_id]
    base_chat = prov["url"]
    base_models = prov.get("models_url", "")
    aliases = HOST_ALIASES.get(provider_id)
    if not aliases:
        yield base_chat, base_models
        return
    # if we already know which host works for this key, use only that
    known = _HOST_OK.get(provider_id)
    hosts = [known] + [h for h in aliases if h != known] if known else list(aliases)
    # derive the host currently in base_chat so we can swap it
    cur_host = re.sub(r"^https?://([^/]+)/.*$", r"\1", base_chat)
    for h in hosts:
        yield (base_chat.replace(cur_host, h, 1),
               base_models.replace(cur_host, h, 1) if base_models else "")

# crude size ranking so "biggest first" still roughly holds for an unknown catalog
def _model_rank(mid):
    s = mid.lower()
    score = 0
    # explicit param-count hints
    m = re.search(r"(\d+)\s*b\b", s) or re.search(r"-(\d+)b", s)
    if m:
        try: score += int(m.group(1))
        except Exception: pass
    # qualitative hints when there's no number
    for kw, pts in (("pro", 300), ("max", 320), ("ultra", 340), ("405", 405), ("671", 671),
                    ("flagship", 350), ("large", 200), ("70", 70), ("32", 32),
                    ("coder", 40), ("instruct", 10),
                    ("flash", -20), ("mini", -40), ("lite", -45), ("small", -50),
                    ("8b", 8), ("7b", 7), ("3b", 3), ("1.5", -10)):
        if kw in s: score += pts
    return score

def fetch_models(provider_id, force=False):
    """Fetch the live list of chat models a provider exposes to this key.
    Returns {"models": [...], "source": "live"|"fallback"|"error", "error": ...}."""
    prov = PROVIDERS.get(provider_id)
    if not prov:
        return {"models": [], "source": "error", "error": "unknown provider"}
    if not force and _MODEL_CACHE.get(provider_id):
        return {"models": _MODEL_CACHE[provider_id], "source": "live"}
    key = STATE.get("keys", {}).get(provider_id, "")
    if not key:
        return {"models": list(prov["models"]), "source": "fallback", "error": "no key yet"}

    last_err = None
    # try each candidate host (e.g. SiliconFlow .com then .cn) until one accepts the key
    for chat_url, models_url in _provider_urls(provider_id):
        if not models_url:
            continue
        host = re.sub(r"^https?://([^/]+)/.*$", r"\1", models_url)
        try:
            req = urllib.request.Request(models_url, headers={
                "Authorization": "Bearer " + key,
                "User-Agent": f"pysmith/{__version__}",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            items = data.get("data", data if isinstance(data, list) else [])
            ids = []
            for it in items:
                mid = it.get("id") if isinstance(it, dict) else str(it)
                if not mid:
                    continue
                low = mid.lower()
                # keep chat/text LLMs only; drop embeddings/rerank/image/audio/video/tts/etc.
                if any(b in low for b in ("embed", "rerank", "bge-", "whisper", "tts", "stt",
                                          "stable-diffusion", "flux", "sdxl", "kolors", "cogvideo",
                                          "wan-", "speech", "audio", "image", "video", "vl-",
                                          "-vl", "vision", "ocr")):
                    continue
                ids.append(mid)
            if not ids:
                last_err = "no chat models returned"
                continue
            ids = sorted(set(ids), key=_model_rank, reverse=True)
            _MODEL_CACHE[provider_id] = ids
            if provider_id in HOST_ALIASES:
                _HOST_OK[provider_id] = host   # remember the host that worked for this key
            return {"models": ids, "source": "live", "host": host}
        except urllib.error.HTTPError as e:
            detail = ""
            try: detail = e.read().decode(errors="replace")[:150]
            except Exception: pass
            if e.code == 401:
                last_err = "key rejected (401)"
                continue   # try the next host — a .cn key 401s on .com and vice versa
            if e.code == 403:
                last_err = "forbidden (403): " + detail
                continue
            last_err = f"HTTP {e.code}" + (": "+detail if detail else "")
        except Exception as e:
            last_err = str(e)

    # nothing worked → fall back to the static list, with a clear reason
    hint = ""
    if provider_id in HOST_ALIASES and last_err and "401" in last_err:
        hint = (" — the key was rejected on every SiliconFlow host (.com and .cn). "
                "Re-copy the key (watch for spaces), or check the account needs verification.")
    return {"models": list(prov["models"]), "source": "fallback",
            "error": (last_err or "could not reach provider") + hint}

def provider_model_chain(provider_id):
    """The model order to try: live catalog if we have it, else the static fallback."""
    return _MODEL_CACHE.get(provider_id) or list(PROVIDERS[provider_id]["models"])

# --------------------------------------------------------------------------
# TOOL LIBRARY  -- persistent, reloadable tools (code + conversation)
# --------------------------------------------------------------------------
LIBRARY_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "pysmith", "library")

def _safe_id(name):
    return re.sub(r"[^A-Za-z0-9_\-]", "_", (name or "tool")).strip("_") or "tool"

def library_save(name, code, messages, version="testing", args="", sid=None):
    """Snapshot a tool to the library at its CURRENT state: its code, the full build
    conversation, the version badge, and the test args. Reopening it restores all of
    that so you continue exactly where you left off — like saving a chat."""
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    tid = _safe_id(name)
    rec = {"id": tid, "name": name or tid, "code": code,
           "messages": messages or [], "version": version or "testing",
           "args": args or "", "toolkit": (detect_toolkit(code or "") or {}).get("label"),
           "from_session": sid, "saved": time.strftime("%Y-%m-%d %H:%M")}
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
                          "saved": r.get("saved"), "toolkit": r.get("toolkit"),
                          "version": r.get("version", "testing"),
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

def session_save(sid, name, code, messages, version="testing", args=""):
    """Auto-save the live conversation+code for a tool in progress (its full state)."""
    os.makedirs(SESSION_DIR, exist_ok=True)
    sid = sid or time.strftime("s%Y%m%d-%H%M%S")
    rec = {"id": sid, "name": name or "untitled", "code": code or "",
           "messages": messages or [], "version": version or "testing", "args": args or "",
           "toolkit": (detect_toolkit(code or "") or {}).get("label"),
           "updated": time.strftime("%Y-%m-%d %H:%M")}
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
                        "updated": r.get("updated"), "toolkit": r.get("toolkit"),
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
# GUI TOOLKITS  -- detect which windowing toolkit a tool uses, and how to get it
# --------------------------------------------------------------------------
# Maps a top-level import to (human label, apt packages to install it on Kali/Debian).
# These are SYSTEM packages installed with apt, NOT pip — installing PyGObject/PyQt via
# pip is fragile, so we never push them through the managed venv.
GUI_TOOLKITS = {
    "gi":      ("GTK (PyGObject)", "python3-gi gir1.2-gtk-3.0"),
    "PyQt5":   ("PyQt5",           "python3-pyqt5"),
    "PyQt6":   ("PyQt6",           "python3-pyqt6"),
    "PySide6": ("PySide6",         "python3-pyside6"),
    "PySide2": ("PySide2",         "python3-pyside2"),
    "tkinter": ("Tkinter",         "python3-tk"),
    "wx":      ("wxPython",        "python3-wxgtk4.0"),
}

def detect_toolkit(code):
    """Return the GUI toolkit a tool uses, or None. Refines the apt line for GTK by
    reading the requested GTK/Adw versions out of the code (3.0 vs 4.0, libadwaita)."""
    tops = set()
    for m in re.finditer(r"^\s*(?:import|from)\s+([a-zA-Z0-9_\.]+)", code, re.M):
        tops.add(m.group(1).split(".")[0])
    for mod, (label, apt) in GUI_TOOLKITS.items():
        if mod in tops:
            if mod == "gi":
                apt_pkgs = ["python3-gi"]
                gtk_ver = re.search(r"require_version\(\s*['\"]Gtk['\"]\s*,\s*['\"]([0-9.]+)['\"]", code)
                apt_pkgs.append("gir1.2-gtk-4.0" if (gtk_ver and gtk_ver.group(1).startswith("4"))
                                else "gir1.2-gtk-3.0")
                if re.search(r"require_version\(\s*['\"]Adw['\"]", code) or "Adw" in code:
                    apt_pkgs.append("gir1.2-adw-1")
                apt = " ".join(apt_pkgs)
            return {"module": mod, "label": label, "apt": apt}
    return None

# --------------------------------------------------------------------------
# DEPENDENCIES  -- detect third-party imports, optionally install into a venv
# --------------------------------------------------------------------------
def detect_deps(code):
    """Return third-party deps split into pip packages and a GUI toolkit (apt).
    GUI toolkits are installed with apt (system), never pip, so they're reported
    separately with the exact apt command."""
    std = getattr(sys, "stdlib_module_names", set())
    obvious = {"os","sys","re","io","json","time","math","socket","subprocess","argparse",
               "itertools","collections","random","hashlib","base64","struct","threading",
               "datetime","pathlib","shutil","csv","urllib","textwrap","glob","tempfile",
               "functools","typing","enum","dataclasses","queue","signal","select","ssl",
               "ipaddress","binascii","zlib","gzip","sqlite3","html","xml","http","email"}
    toolkit_mods = set(GUI_TOOLKITS.keys())
    pip = set()
    for m in re.finditer(r"^\s*(?:import|from)\s+([a-zA-Z0-9_\.]+)", code, re.M):
        top = m.group(1).split(".")[0]
        if (top and top not in std and top not in obvious
                and top not in toolkit_mods and not top.startswith("_")):
            pip.add(top)
    tk = detect_toolkit(code)
    return {"pip": sorted(pip), "toolkit": tk}


VENV_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "pysmith", "venv")

def install_deps(pkgs):
    """Install pure-Python (pip) packages into pysmith's managed venv. Returns log +
    the python path. The venv is created WITH access to system site-packages so a tool
    can use both pip packages here AND the system GUI toolkit (PyGObject/PyQt)."""
    if not pkgs:
        return {"ok": True, "log": "no pip packages — pure stdlib", "python": sys.executable}
    try:
        if not os.path.isdir(VENV_DIR):
            import venv
            # system_site_packages=True so the venv can still import the apt-installed
            # GUI toolkit (gi/PyQt5/...) which can't be pip-installed reliably.
            venv.EnvBuilder(with_pip=True, system_site_packages=True).create(VENV_DIR)
        vpy = os.path.join(VENV_DIR, "bin", "python")
        if not os.path.exists(vpy):
            vpy = os.path.join(VENV_DIR, "Scripts", "python.exe")  # windows fallback
        proc = subprocess.run([vpy, "-m", "pip", "install", *pkgs],
                              capture_output=True, text=True, timeout=300)
        out = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": proc.returncode == 0, "log": out[-1500:], "python": vpy}
    except Exception as e:
        return {"ok": False, "log": f"venv/install failed: {e}", "python": sys.executable}

def install_apt(apt_pkgs):
    """Install system GUI packages with apt. Needs sudo; on Kali this is the right way
    to get PyGObject/PyQt/etc. Returns a log. Best-effort and clearly reports failures."""
    pkgs = [p for p in (apt_pkgs or "").split() if p]
    if not pkgs:
        return {"ok": True, "log": "no system packages needed"}
    import shutil as _sh
    if not _sh.which("apt-get") and not _sh.which("apt"):
        return {"ok": False, "log": "apt not found — this looks like a non-Debian system. "
                                    "Install the toolkit with your package manager instead."}
    apt_bin = _sh.which("apt-get") or _sh.which("apt")
    cmd = ["sudo", apt_bin, "install", "-y", *pkgs] if os.geteuid() != 0 else [apt_bin, "install", "-y", *pkgs]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        out = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
        if not ok and "sudo" in cmd[0]:
            out += ("\n[pysmith] If sudo needs a password it can't be entered here. "
                    "Run this in your terminal:\n  sudo " + apt_bin + " install -y " + " ".join(pkgs))
        return {"ok": ok, "log": out[-1800:]}
    except Exception as e:
        return {"ok": False, "log": f"apt install failed: {e}\nRun manually:\n  sudo {apt_bin} install -y {' '.join(pkgs)}"}

# the interpreter used to run tools. For GUI tools we MUST use the system interpreter
# (sys.executable) so the apt-installed toolkit (gi/PyQt/...) is importable; the managed
# venv is only used for pure-Python pip deps (and is created with system site-packages,
# so it can see the toolkit too).
def run_python(code=None):
    venv_py = None
    for cand in (os.path.join(VENV_DIR, "bin", "python"),
                 os.path.join(VENV_DIR, "Scripts", "python.exe")):
        if os.path.exists(cand):
            venv_py = cand
            break
    # If the code needs pip deps, prefer the venv (which also sees system packages).
    # Otherwise use the plain system interpreter so the GUI toolkit is always present.
    if code is not None:
        d = detect_deps(code)
        if d["pip"] and venv_py:
            return venv_py
        return sys.executable
    return venv_py or sys.executable

# ==========================================================================
# helpers
# ==========================================================================
def looks_dangerous(code):
    return [p for p in DANGER if re.search(p, code)]

def _http_post(url, headers, body, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

# --------------------------------------------------------------------------
# CONTEXT BUDGET  -- keep requests under the ACTIVE model's real window
# --------------------------------------------------------------------------
# The previous version used one fixed budget (120k chars). That was the bug behind
# "works on a fresh tool, dies after long use": a long session would fall through
# the model chain to a SMALL-context model (e.g. an 8k-token model) for which 120k
# chars is wildly over the limit — so the request 400'd even though trimming "ran".
# Now we budget against the specific model being called.
#
# Context windows in TOKENS (input side). ~3.5 chars/token for code-heavy text, and
# we reserve room for the reply, so usable input chars ≈ tokens * 3. Unknown models
# get a conservative default so we never overshoot a small one.
MODEL_CONTEXT_TOKENS = {
    # Groq
    "llama-3.3-70b-versatile": 128000, "openai/gpt-oss-120b": 128000,
    "openai/gpt-oss-20b": 128000, "gemma2-9b-it": 8192, "llama-3.1-8b-instant": 128000,
    # SiliconFlow
    "deepseek-ai/deepseek-v3": 64000, "qwen/qwen2.5-72b-instruct": 32000,
    "qwen/qwen2.5-coder-32b-instruct": 32000, "deepseek-ai/deepseek-v2.5": 32000,
    "qwen/qwen2.5-7b-instruct": 32000,
    # Google
    "gemini-2.5-pro": 1000000, "gemini-2.5-flash": 1000000, "gemini-2.0-flash": 1000000,
    "gemini-1.5-pro": 2000000, "gemini-1.5-flash": 1000000,
    # Novita
    "deepseek/deepseek-v3": 64000, "qwen/qwen-2.5-72b-instruct": 32000,
    "meta-llama/llama-3.1-70b-instruct": 128000, "meta-llama/llama-3.1-8b-instruct": 128000,
}
DEFAULT_CONTEXT_TOKENS = 16000      # safe assumption for an unknown model
REPLY_RESERVE_TOKENS   = 4000       # leave room for the model's answer

def context_budget_chars(model):
    """Usable input-char budget for a specific model, conservatively converted from
    its token window with headroom reserved for the reply."""
    toks = MODEL_CONTEXT_TOKENS.get((model or "").lower(), DEFAULT_CONTEXT_TOKENS)
    usable = max(2000, toks - REPLY_RESERVE_TOKENS)
    # ~3 input chars per token (conservative for code), capped so we never send an
    # absurdly huge request even to a million-token model (keeps latency/cost sane).
    return min(usable * 3, 300_000)

def _msg_len(m):
    return len(m.get("content", "") or "")

# matches a fenced code block so we can collapse superseded copies
_CODE_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n.*?```", re.S)

def trim_history(messages, model=None):
    """Keep a long build conversation under the ACTIVE model's window without losing
    what matters. Two-stage:
      1. COLLAPSE every OLD assistant code block into a one-line placeholder — only the
         most recent full script is kept verbatim. (This is the real fix: long sessions
         accumulate many full copies of the same growing program, and that redundancy,
         not the chat, is what blows the context window.)
      2. If still over budget, drop the stale middle of the conversation, keeping the
         system prompt, the current code, and the most recent turns; leave a marker.
    """
    if not messages:
        return messages
    budget_total = context_budget_chars(model)

    system = [m for m in messages if m.get("role") == "system"]
    body   = [m for m in messages if m.get("role") != "system"]

    # ---- stage 1: collapse superseded code blocks ----
    last_code_idx = None
    for i in range(len(body) - 1, -1, -1):
        if body[i].get("role") == "assistant" and "```" in (body[i].get("content") or ""):
            last_code_idx = i
            break
    if last_code_idx is not None:
        for i in range(len(body)):
            if i == last_code_idx:
                continue
            m = body[i]
            if m.get("role") == "assistant" and "```" in (m.get("content") or ""):
                collapsed = _CODE_FENCE.sub("`[earlier version of the code — superseded by the latest below]`",
                                            m["content"])
                body[i] = {"role": m["role"], "content": collapsed}

    sys_len = sum(_msg_len(m) for m in system)
    budget  = budget_total - sys_len
    total   = sum(_msg_len(m) for m in body)
    if total <= budget:
        return system + body   # stage-1 collapse alone got us under the limit

    # ---- stage 2: drop the stale middle, force-keeping the current code ----
    # recompute the code index after collapse (it didn't move)
    kept_tail, used = [], 0
    for i in range(len(body) - 1, -1, -1):
        m = body[i]
        L = _msg_len(m)
        if used + L <= budget or not kept_tail:
            kept_tail.append(m); used += L
        elif i == last_code_idx:
            content = m.get("content") or ""
            if L > budget:
                content = content[: max(2000, budget - 200)] + "\n# …(truncated by pysmith to fit this model)…"
            kept_tail.append({"role": m["role"], "content": content}); used += min(L, budget)
        else:
            continue
    kept_tail.reverse()

    dropped = len(body) - len(kept_tail)
    marker = []
    if dropped > 0:
        marker = [{"role": "user", "content":
                   f"(pysmith note: {dropped} earlier message(s) were trimmed to fit this model's "
                   f"context window. The current code and recent discussion are below; treat the "
                   f"latest code block as the source of truth.)"}]
    return system + marker + kept_tail

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

    # raw history; trimmed PER MODEL inside the loop (each model has its own window)
    raw_messages = messages

    # model order: a user-chosen model (if set) first, then the rest of the LIVE chain
    chosen = STATE.get("models", {}).get(pid)
    chain = provider_model_chain(pid)
    if chosen:
        chain = [chosen] + [m for m in chain if m != chosen]

    # which host to call: the one fetch_models proved works for this key, else the
    # configured one. (Handles SiliconFlow .com vs .cn automatically.)
    chat_url = prov["url"]
    for cu, _mu in _provider_urls(pid):
        chat_url = cu
        break

    last = None
    context_hit = False
    _retried_host = [False]   # one-shot host re-discovery guard (mutable for closure-free use)
    for model in chain:
        # trim to THIS model's context window — the fix for "dies after long use":
        # a small-context model deeper in the chain now gets a request sized for it.
        messages = trim_history(raw_messages, model)
        # pre-flight: if even the trimmed payload won't fit this model (e.g. system
        # prompt + current code alone exceeds a tiny 8k window), skip it instead of
        # sending a request we know will 400. A bigger model later in the chain may fit.
        if sum(_msg_len(m) for m in messages) > context_budget_chars(model):
            last = f"{model}: skipped (payload exceeds its context window)"
            context_hit = True
            continue
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
                "User-Agent": f"pysmith/{__version__}",
                "Accept": "application/json",
            }
            body = {"model": model, "temperature": 0.3, "messages": messages}
            data = _http_post(chat_url, headers, body)
            reply = data["choices"][0]["message"]["content"]
            return {"reply": reply, "model": model, "provider": pid}
        except urllib.error.HTTPError as e:
            detail = ""
            try: detail = e.read().decode(errors="replace")[:400]
            except Exception: pass
            low = detail.lower()
            # --- the conversation got too big for this model's context window ---
            if (e.code in (400, 413) and any(s in low for s in (
                    "context", "token", "maximum context", "too long", "context_length",
                    "context length", "max_tokens", "reduce the length", "input is too long"))):
                context_hit = True
                last = f"{model}: context-window limit"
                continue   # a smaller-context sibling won't help, but try in case limits differ
            if e.code == 403 and "1010" in detail:
                return {"error": f"Blocked by Cloudflare (403/1010) before reaching "
                                 f"{prov['label']}. Usually a VPN/proxy or outdated client, not your key."}
            if e.code == 401:
                # For a multi-host provider (SiliconFlow .com/.cn), a 401 may just mean
                # we're hitting the wrong regional host for this key. Discover the right
                # one and retry this same request once.
                if pid in HOST_ALIASES and not _retried_host[0]:
                    _retried_host[0] = True
                    probe = fetch_models(pid, force=True)
                    if probe.get("source") == "live" and _HOST_OK.get(pid):
                        new_url = None
                        for cu, _mu in _provider_urls(pid):
                            new_url = cu; break
                        if new_url and new_url != chat_url:
                            chat_url = new_url
                            # retry the very same model against the correct host
                            try:
                                body = {"model": model, "temperature": 0.3, "messages": messages}
                                data = _http_post(chat_url, headers, body)
                                reply = data["choices"][0]["message"]["content"]
                                return {"reply": reply, "model": model, "provider": pid}
                            except Exception as e2:
                                last = f"{model}: retry on {_HOST_OK[pid]} failed: {e2}"
                                continue
                return {"error": f"{prov['label']} rejected the key (401). Check it in Settings — "
                                 f"and confirm you're using a {prov['label']} key, not another provider's."
                                 + (f" For SiliconFlow, the key must be from the same site as the "
                                    f"endpoint (cloud.siliconflow.com ↔ api.siliconflow.com)."
                                    if pid == "siliconflow" else "")}
            if e.code == 429:
                return {"error": f"{prov['label']} rate-limited this request (429): "
                                 f"{detail or 'slow down or check your quota'}."}
            if e.code in (404, 400):
                # this specific model name isn't callable with your key — try the next
                last = f"{model}: HTTP {e.code} (this model isn't available to your {prov['label']} key)"
                continue
            last = f"{model}: HTTP {e.code} {detail}"
        except Exception as e:
            last = f"{model}: {e}"

    if context_hit:
        return {"error": "context_overflow",
                "detail": "Your current tool plus the build conversation is too large for the "
                          "available model(s). pysmith already collapses old code revisions and "
                          "trims old turns automatically, so this means the tool itself is now very "
                          "big. Two fixes: pick a larger-context model in Settings (Gemini and the "
                          "70B/120B models have huge windows), or hit ＋ new tool to start fresh — "
                          "your saved work in the library is untouched. You can also save the current "
                          "tool to the library first, then reopen it in a clean session to keep going."}
    return {"error": f"{prov['label']} chain failed. Last: {last}. "
                     f"Try Settings → refresh models, or pick a different model/provider."}

def extract_code(reply):
    """Pull the python code block out of a model reply (tagged, else any fence)."""
    m = re.search(r"```(?:python|py)\s*\n(.*?)```", reply, re.S | re.I) \
        or re.search(r"```\s*\n(.*?)```", reply, re.S)
    return m.group(1).rstrip() if m else None

def smoke_test(code):
    """Silent quality checks on generated code. Returns (passed, report, checks).
    IMPORTANT: this only checks that the code PARSES and IMPORTS cleanly. It does
    NOT open the window — doing that needs a display and would block. For GUI tools
    it also verifies the code is import-safe (no window opens at import time) and is
    TOLERANT of a headless/toolkit-less test box: a missing display or missing GUI
    typelib is an environment fact here, not a bug in the generated tool. Real
    behaviour is verified by the user pressing Run on their Kali machine."""
    checks = []
    # 1. syntax
    try:
        import ast
        ast.parse(code)
        checks.append(("syntax", True, ""))
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}", [("syntax", False, str(e))]

    tk = detect_toolkit(code)

    # 1b. import-safety for GUI tools: building/running the GUI must be guarded by
    #     `if __name__ == "__main__":` (or a main() called only there), so importing
    #     the module doesn't try to open a window. Catch the obvious mistake of a
    #     top-level mainloop/run/show call.
    if tk:
        bad = re.search(r"^\s*(?:Gtk\.main\(\)|app\.run\(|window\.show_all\(\)|"
                        r"\w+\.mainloop\(\)|sys\.exit\(\s*app\.exec)", code, re.M)
        if bad and "__main__" not in code:
            msg = ("GUI tool isn't import-safe: it opens/runs the window at module top "
                   "level. Move all window construction and the main loop inside "
                   "`if __name__ == \"__main__\":`.")
            checks.append(("import-safe", False, msg))
            return False, msg, checks
        checks.append(("import-safe", True, ""))

    # 2. import-ability: load the module WITHOUT running its __main__ block.
    fd, path = tempfile.mkstemp(prefix="pysmith_test_", suffix=".py")
    # signatures meaning "this box just can't load the GUI" — never a code bug
    ENV_SIGNS = ("Namespace", "not available", "cannot open display", "could not open display",
                 "couldn't connect to display", "no display name", "Unable to init server",
                 "Gtk couldn't be initialized", "GtkInitError", "QXcbConnection",
                 "qt.qpa.plugin", "no Qt platform plugin", "xcb", "DISPLAY",
                 "_tkinter.TclError", "libGL", "Gdk")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)
        harness = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('pysmith_candidate', {path!r})\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "try:\n"
            "    spec.loader.exec_module(mod)\n"
            "except (ModuleNotFoundError, ImportError) as e:\n"
            "    print('DEP_MISSING:' + str(e)); sys.exit(0)\n"
            "except SystemExit as e:\n"
            "    print('TOOLKIT_EXIT:' + str(e)); sys.exit(0)\n"
            "except BaseException as e:\n"
            "    import traceback; tb = traceback.format_exc()\n"
            "    sys.stderr.write(tb)\n"
            "    sys.exit(7)\n"
        )
        try:
            proc = subprocess.run([sys.executable, "-c", harness],
                                  capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
            out = proc.stdout.decode("utf-8", errors="replace")
            err = proc.stderr.decode("utf-8", errors="replace")
            blob = out + "\n" + err
            if out.startswith("DEP_MISSING:"):
                note = "needs a package (use the deps button)"
                if tk:
                    note = f"needs the {tk['label']} toolkit — apt: {tk['apt']}"
                checks.append(("imports", True, note))
            elif out.startswith("TOOLKIT_EXIT:") or (tk and any(s in blob for s in ENV_SIGNS)):
                # the tool bailed gracefully because the toolkit/display isn't on THIS box,
                # or hit an environment-only error. Structurally fine.
                checks.append(("imports", True, "toolkit/display not present on the test box "
                                                 "(expected — runs on your Kali machine)"))
            elif proc.returncode != 0:
                # a genuine error at import/definition time (NameError, bad default, etc.)
                msg = err.strip()[-500:] or "import failed"
                checks.append(("imports", False, msg))
                return False, msg, checks
            else:
                checks.append(("imports", True, ""))
        except subprocess.TimeoutExpired:
            checks.append(("imports", False, "import timed out (top-level code is blocking — "
                                             "is a window opening at import time?)"))
            return False, "Import timed out — there may be blocking/GUI code at module top level.", checks
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

# Live GUI processes launched by Run, so we can report status and stop them.
# {pid: {"proc": Popen, "name": str, "path": tmpfile, "started": ts}}
RUNNING = {}
_RUNNING_LOCK = threading.Lock()

def _reap():
    """Drop finished processes and clean up their temp files."""
    with _RUNNING_LOCK:
        for pid in list(RUNNING):
            info = RUNNING[pid]
            if info["proc"].poll() is not None:
                try: os.unlink(info["path"])
                except Exception: pass
                RUNNING.pop(pid, None)

def list_running():
    _reap()
    with _RUNNING_LOCK:
        return {"running": [{"pid": pid, "name": i["name"],
                             "seconds": round(time.time() - i["started"], 1)}
                            for pid, i in RUNNING.items()]}

def stop_running(pid):
    """Terminate a launched GUI (and its children)."""
    _reap()
    with _RUNNING_LOCK:
        info = RUNNING.get(pid)
    if not info:
        return {"ok": False, "error": "not running (already closed?)"}
    proc = info["proc"]
    try:
        # kill the whole process group if we made one
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except Exception:
            proc.terminate()
        try: proc.wait(timeout=3)
        except Exception:
            try: os.killpg(os.getpgid(proc.pid), 9)
            except Exception: proc.kill()
        return {"ok": True, "pid": pid}
    finally:
        _reap()

def run_code(code, args, confirmed, name="tool"):
    danger = looks_dangerous(code)
    if danger and not confirmed:
        return {"needsConfirm": True, "patterns": danger}

    # parse args the way a shell would (handles quotes/spaces), not naive split
    try:
        argv = shlex.split(args) if args else []
    except ValueError as e:
        return {"stdout": "", "stderr": f"Couldn't parse arguments: {e}", "exit": -1, "seconds": 0}

    tk = detect_toolkit(code)
    interp = run_python(code)

    # unique temp file per run so concurrent/rapid runs can't clobber each other.
    # GUI launches keep their file alive until the window closes (cleaned up by _reap).
    fd, path = tempfile.mkstemp(prefix="pysmith_", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(code)

    # ----- GUI tool: LAUNCH it (don't block on the window) -----------------
    if tk:
        _reap()
        # peek at the first ~1.8s of stderr to catch immediate failures
        # (missing toolkit, missing display, a crash on startup), then leave it running.
        try:
            errf = tempfile.NamedTemporaryFile(prefix="pysmith_err_", suffix=".log", delete=False)
            t0 = time.time()
            proc = subprocess.Popen(
                [interp, path] + argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errf,
                start_new_session=True,   # own process group -> clean stop later
            )
        except Exception as e:
            try: os.unlink(path)
            except Exception: pass
            return {"stdout": "", "stderr": f"Could not launch: {e}", "exit": -1, "seconds": 0}

        time.sleep(1.8)
        rc = proc.poll()
        try:
            errf.flush(); errf.close()
            with open(errf.name, "rb") as ef:
                early_err = ef.read().decode("utf-8", errors="replace")
        except Exception:
            early_err = ""
        finally:
            try: os.unlink(errf.name)
            except Exception: pass

        if rc is not None and rc != 0:
            # died on startup — diagnose toolkit / display problems precisely
            hint = ""
            if tk and ("Namespace" in early_err or "ModuleNotFoundError" in early_err
                       or "ImportError" in early_err or "not available" in early_err):
                hint = (f"\n[pysmith] The {tk['label']} toolkit isn't installed. Install it:\n"
                        f"  sudo apt install {tk['apt']}\n"
                        f"(or click the ⬇ deps button, which can do this for you).")
            elif any(s in early_err for s in ("cannot open display", "no display name",
                      "Unable to init server", "QXcbConnection", "no Qt platform plugin",
                      "Gtk couldn't be initialized", "could not open display",
                      "couldn't connect to display", "DISPLAY")):
                hint = ("\n[pysmith] The GUI couldn't open a window — no display is available. "
                        "Launch pysmith from inside your Kali desktop session (KDE or Phosh), "
                        "not over a plain SSH shell. The tool itself looks fine.")
            try: os.unlink(path)
            except Exception: pass
            return {"stdout": "", "stderr": (early_err or "the GUI exited immediately") + hint,
                    "exit": rc, "seconds": round(time.time() - t0, 2), "gui": True}

        if rc is not None and rc == 0:
            # opened and closed cleanly within the peek window (or it's a one-shot)
            try: os.unlink(path)
            except Exception: pass
            return {"stdout": "", "stderr": early_err, "exit": 0,
                    "seconds": round(time.time() - t0, 2), "gui": True, "launched": False,
                    "note": "ran and exited cleanly"}

        # still running -> success: the window is open on the user's screen
        with _RUNNING_LOCK:
            RUNNING[proc.pid] = {"proc": proc, "name": name or "tool", "path": path, "started": t0}
        return {"stdout": "", "stderr": early_err, "exit": 0,
                "seconds": round(time.time() - t0, 2), "gui": True, "launched": True,
                "pid": proc.pid,
                "note": f"{tk['label']} window launched (pid {proc.pid}). It's open on your "
                        f"desktop — interact with it there. Use ■ stop to close it."}

    # ----- non-GUI fallback (rare now): capture output as before ----------
    try:
        t0 = time.time()
        try:
            proc = subprocess.run(
                [interp, path] + argv,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=120)
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "Killed: exceeded 120s (possible infinite loop, "
                    "or the tool was waiting for input — pysmith provides none).",
                    "exit": -1, "seconds": round(time.time() - t0, 2)}
        except Exception as e:
            return {"stdout": "", "stderr": f"Could not launch: {e}", "exit": -1, "seconds": 0}

        out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        errtxt = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
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
    tk = detect_toolkit(code)
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
            launch = f"Launch from your app grid, or run:\n\n```bash\npython3 {name}.py\n```"
            apt_note = f"\n\nNeeds: `sudo apt install {tk['apt']}`" if tk else ""
            with open(readme, "w") as f:
                f.write(f"# {name}\n\nA graphical tool built with pysmith.{apt_note}\n\n## Usage\n\n{launch}\n")
        # a .desktop launcher so a GUI tool appears in KDE / Phosh
        if tk:
            dt = os.path.join(d, name + ".desktop")
            with open(dt, "w") as f:
                f.write("[Desktop Entry]\nType=Application\n"
                        f"Name={name}\nComment=Built with pysmith\n"
                        f"Exec=python3 {pyp}\nTerminal=false\nCategories=Utility;Security;\n")
        return {"path": d, "toolkit": tk["label"] if tk else None}
    else:
        d = os.path.join(base, "forge")
        os.makedirs(d, exist_ok=True)
        pyp = os.path.join(d, name + ".py")
        with open(pyp, "w") as f:
            f.write(code + "\n")
        try: os.chmod(pyp, 0o755)
        except Exception: pass
        return {"path": pyp, "toolkit": tk["label"] if tk else None}

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

def _install_sh(user, repo, branch, name, apt_deps=""):
    """A smart one-file installer that works via curl|bash or from a clone. Installs the
    GUI toolkit (apt), the script, a CLI launcher, AND a .desktop entry so the tool shows
    up in the KDE / Phosh app grid."""
    apt_line = ""
    if apt_deps.strip():
        apt_line = f'''
# install the GUI toolkit this tool needs
APT_PKGS="{apt_deps.strip()}"
if command -v apt-get >/dev/null 2>&1; then
  echo "installing toolkit: $APT_PKGS (may prompt for sudo)…"
  sudo apt-get update -qq || true
  sudo apt-get install -y $APT_PKGS || echo "WARN: could not auto-install $APT_PKGS — install them manually"
else
  echo "NOTE: install these with your package manager: $APT_PKGS"
fi
'''
    return f"""#!/usr/bin/env bash
# {repo} installer — one-line install/update over HTTPS:
#   curl -fsSL https://raw.githubusercontent.com/{user}/{repo}/{branch}/install.sh | bash
set -euo pipefail
REPO="{user}/{repo}"; BRANCH="{branch}"
SRC="$HOME/.local/share/{repo}"; BIN="$HOME/.local/bin"; LAUNCH="$BIN/{name}"
APPS="$HOME/.local/share/applications"; ICONS="$HOME/.local/share/icons"

command -v python3 >/dev/null 2>&1 || {{ echo "python3 required: sudo apt install python3"; exit 1; }}
{apt_line}
mkdir -p "$SRC" "$BIN" "$APPS" "$ICONS"
SELF_DIR="$( cd "$( dirname "${{BASH_SOURCE[0]:-$0}}" )" 2>/dev/null && pwd || true )"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/{name}.py" ]; then
  cp -f "$SELF_DIR/{name}.py" "$SRC/"
  [ -f "$SELF_DIR/requirements.txt" ] && cp -f "$SELF_DIR/requirements.txt" "$SRC/" || true
  [ -f "$SELF_DIR/{name}.png" ] && cp -f "$SELF_DIR/{name}.png" "$ICONS/" || true
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

# install pure-python deps if any
[ -f "$SRC/requirements.txt" ] && python3 -m pip install -r "$SRC/requirements.txt" --break-system-packages -q 2>/dev/null || true

# CLI launcher (uses system python3 so the system toolkit is importable)
cat > "$LAUNCH" <<EOF
#!/usr/bin/env bash
exec python3 "$SRC/{name}.py" "\\$@"
EOF
chmod +x "$LAUNCH"

# desktop entry -> appears in the KDE / Phosh app grid
cat > "$APPS/{name}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name={name}
Comment={repo} — built with pysmith
Exec=python3 $SRC/{name}.py
Terminal=false
Categories=Utility;Security;
EOF
update-desktop-database "$APPS" >/dev/null 2>&1 || true

case ":$PATH:" in *":$BIN:"*) ;; *)
  RC="$HOME/.bashrc"; [ -n "${{ZSH_VERSION:-}}" ] && RC="$HOME/.zshrc"
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"
  echo "added $BIN to PATH in $RC — run: source $RC" ;;
esac
echo "installed. launch from your app grid, or run: {name}"
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
        f"# {repo}\n\n{gh.get('description','A graphical Python tool built with pysmith.')}\n\n"
        f"## Install\n\n```bash\ncurl -fsSL https://raw.githubusercontent.com/{user}/{repo}/{branch}/install.sh | bash\n```\n\n"
        f"## Usage\n\nLaunch it from your app grid (KDE / Phosh), or run:\n\n```bash\n{name}\n```\n")
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

    # install.sh (installs the GUI toolkit via apt + a .desktop launcher)
    apt_deps = (gh.get("apt") or "").strip()
    if not apt_deps:
        tk = detect_toolkit(code)
        apt_deps = tk["apt"] if tk else ""
    ish = os.path.join(d, "install.sh")
    with open(ish, "w") as f:
        f.write(_install_sh(user, repo, branch, name, apt_deps))
    try: os.chmod(ish, 0o755)
    except Exception: pass

    # .desktop entry so the GUI tool appears in the KDE / Phosh app grid
    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment={gh.get('description', repo + ' — built with pysmith')}\n"
        f"Exec=python3 %h/.local/share/{repo}/{name}.py\n"
        "Terminal=false\n"
        "Categories=Utility;Security;\n"
    )
    with open(os.path.join(d, name + ".desktop"), "w") as f:
        f.write(desktop)

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
            provs = []
            for pid, p in PROVIDERS.items():
                chain = provider_model_chain(pid)   # live if cached, else fallback
                provs.append({"id": pid, "label": p["label"],
                              "hasKey": bool(STATE["keys"].get(pid)),
                              "models": chain,
                              "chosen": STATE["models"].get(pid) or (chain[0] if chain else "?"),
                              "topModel": chain[0] if chain else "?",
                              "live": pid in _MODEL_CACHE})
            cur_chain = provider_model_chain(STATE["provider"])
            chosen_cur = STATE["models"].get(STATE["provider"]) or (cur_chain[0] if cur_chain else "?")
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
        elif self.path == "/api/running":
            self._send(200, list_running())
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
            # a new key means we can now ask the provider what it actually offers
            fetched = None
            if STATE["keys"][pid]:
                _MODEL_CACHE.pop(pid, None)
                _HOST_OK.pop(pid, None)
                fetched = fetch_models(pid, force=True)
            self._send(200, {"hasKey": bool(STATE["keys"][pid]), "saved": saved,
                             "models": (fetched or {}).get("models"),
                             "modelSource": (fetched or {}).get("source"),
                             "modelError": (fetched or {}).get("error")})
        elif self.path == "/api/provider":
            pid = data.get("provider")
            if pid not in PROVIDERS:
                return self._send(200, {"error": "unknown provider"})
            STATE["provider"] = pid
            persist_state()
            chain = provider_model_chain(pid)
            self._send(200, {"provider": pid, "hasKey": bool(STATE["keys"].get(pid)),
                             "model": STATE["models"].get(pid) or (chain[0] if chain else "?")})
        elif self.path == "/api/models/refresh":
            pid = data.get("provider") or STATE["provider"]
            if pid not in PROVIDERS:
                return self._send(200, {"error": "unknown provider"})
            self._send(200, {"provider": pid, **fetch_models(pid, force=True)})
        elif self.path == "/api/model":
            pid = data.get("provider") or STATE["provider"]
            model = data.get("model")
            if pid not in PROVIDERS:
                return self._send(200, {"error": "unknown provider"})
            # accept any model from the live catalog OR the static fallback
            valid = set(provider_model_chain(pid)) | set(PROVIDERS[pid]["models"])
            if model and model in valid:
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
                              bool(data.get("confirm")), data.get("name", "tool"))
            # log only actual runs (not the confirm-gate response)
            if "needsConfirm" not in result:
                log_run(data.get("name", "tool"), data.get("args", ""), result)
            self._send(200, result)
        elif self.path == "/api/stop":
            self._send(200, stop_running(int(data.get("pid", 0) or 0)))
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
                                         data.get("messages", []),
                                         data.get("version", "testing"),
                                         data.get("args", ""), data.get("sessionId")))
        elif self.path == "/api/library/load":
            self._send(200, library_load(data.get("id", "")))
        elif self.path == "/api/library/delete":
            self._send(200, library_delete(data.get("id", "")))
        elif self.path == "/api/session/save":
            self._send(200, session_save(data.get("id"), data.get("name", "untitled"),
                                         data.get("code", ""), data.get("messages", []),
                                         data.get("version", "testing"), data.get("args", "")))
        elif self.path == "/api/session/load":
            self._send(200, session_load(data.get("id", "")))
        elif self.path == "/api/session/delete":
            self._send(200, session_delete(data.get("id", "")))
        elif self.path == "/api/deps":
            self._send(200, detect_deps(data.get("code", "")))
        elif self.path == "/api/deps/install":
            self._send(200, install_deps(data.get("pip", []) or data.get("deps", [])))
        elif self.path == "/api/apt/install":
            self._send(200, install_apt(data.get("apt", "")))
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
    print(f"  building: graphical tools for Kali (KDE + Phosh)")
    have = [PROVIDERS[pid]["label"] for pid in PROVIDERS if STATE["keys"].get(pid)]
    if have:
        print(f"  keys loaded for: {', '.join(have)}")
        # fetch each keyed provider's live model catalog in the background so the
        # dropdown is accurate without blocking startup
        def _warm():
            for pid in PROVIDERS:
                if STATE["keys"].get(pid):
                    fetch_models(pid, force=True)
        threading.Thread(target=_warm, daemon=True).start()
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

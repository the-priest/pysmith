<div align="center">

<img src="assets/icon.png" width="120" alt="pysmith">

# pysmith

**An AI workspace for building Python GUI tools — not a chatbot.**
Talk through the tool, agree on what it should do, get a **testing version that opens a real
window you launch right there**, iterate on real behaviour, and only when you're happy does it
package a **release version** for GitHub. Every tool is a graphical app tuned to run on Kali
Linux under both **KDE Plasma** (desktop) and **Phosh** (phone / NetHunter Pro).

`local app` · `Groq + more` · `GTK / Qt / Tk` · `your key never leaves your machine`

</div>

---

## Install

One line:

```bash
curl -fsSL https://raw.githubusercontent.com/the-priest/pysmith/main/install.sh | bash
```

Installs under `$HOME` (no root): the app into `~/.local/share/pysmith`, a `pysmith`
launcher on your `PATH`, and an icon + menu entry. Then:

```bash
export GROQ_API_KEY="gsk_your_key_here"
```

```bash
pysmith
```

It starts a small local server and opens the workspace in your browser at
`http://127.0.0.1:8765`. (No key in your env? Just open **Settings** in the app and paste
it — it's held in memory for the session, never written to disk.)

### From a clone

```bash
git clone https://github.com/the-priest/pysmith.git
```

```bash
cd pysmith && ./install.sh
```

Or just run it without installing:

```bash
python3 pysmith.py
```

---

## How it works

When you describe a new tool, pysmith first runs a **structured intake** — the model
generates a short set of tap-to-answer questions tailored to *that* tool (toolkit, target
screen, what the window shows, dependencies, etc.), then builds precisely to your choices.
Every build is a single-file **GUI app**: adaptive layout that works on a wide KDE window or a
narrow ~360px Phosh phone screen, threaded so it never freezes, with graceful errors when a
Kali binary or the toolkit itself is missing. A stronger engineering system prompt plus the
auto-test loop means even smaller models produce working, defensive code.

The default toolkit is **GTK 3 via PyGObject** — the native Phosh stack that also runs fine
under KDE — but you can pick PyQt5 or Tkinter in the intake.

When you click **◆ Get ready for GitHub**, pysmith asks your username, repo name, branch,
and license, then polishes the code and assembles a complete repo: a README with a one-line
HTTPS install/update command, `install.sh` (which installs the GUI toolkit via `apt` and a
`.desktop` launcher so the tool lands in your app grid), `LICENSE`, `.gitignore`, and the exact
`git` push commands (HTTPS, never SSH).

### The workspace

It's a workspace, built around the **code and launching it** — not a wall of chat.

- **Build dialogue** (left): describe the tool. pysmith agrees on scope first — it'll ask a
  sharp question or lay out a quick plan before writing anything. **⎘ attach** a `.py` to load
  it as the working tool, or attach logs / configs / sample data as context for the build.
- **Tool pane** (right, top): the current script, syntax-highlighted, with a
  `TESTING` / `RELEASE` badge.
- **Test console** (right, bottom): hit **▶ launch** and the GUI actually opens on your desktop.
  pysmith catches startup problems (missing toolkit, no display, a crash) and reports them here;
  **■ stop** closes the window. Double-click an error to fire it straight back into the dialogue
  for a fix.
- **⬇ deps**: detects the GUI toolkit (installs it with `apt`) and any pip packages (into a
  managed venv) so the window can actually open.
- **★ library**: snapshots the tool **at its current state** — code, the whole build
  conversation, version badge and launch args — exactly like saving a chat. Reopen it later and
  pick up precisely where you left off. Works-in-progress also auto-save to the **in progress**
  tab.
- **◆ Build release version**: when you're happy, this asks the model for the polished,
  GitHub-ready form, plus a README and a `.desktop` launcher.
- **⤓ Save**: testing versions go to `~/pysmith-tools/forge/`, release versions to
  `~/pysmith-tools/release/<name>/` (with a `.desktop` entry).

The model is taught a **method**, not just "write code": agree first, ship a working testing
GUI by default, iterate on real behaviour, and only produce the release version when
asked. That's the `SYSTEM_PROMPT` at the top of `pysmith.py` — yours to tune.

---

## Safety

- **Never auto-runs.** Code only executes when you press the button.
- **Destructive-pattern scan.** Drafts matching `rm -rf /`, fork bombs, `dd if=`, `mkfs`,
  rmtree-on-home, etc. trigger a red confirmation gate before they can run.
- **Local only.** The server binds `127.0.0.1` — nothing is exposed to the network. Your
  Groq key stays in the local process and is never sent to the browser.

**▶ launch** runs the GUI as **your** user with no sandbox — that's the point (you're testing
real tools), so take the review step seriously.

---

## Configuration

Top of `pysmith.py`:

- **`PROVIDERS`** — the providers pysmith can call (Groq, SiliconFlow, Google AI Studio,
  Novita) and a fallback model chain for each. You pick the active one per session from the
  dropdown; a failed call falls through that provider's chain. **You normally don't edit the
  model lists** — pysmith fetches each provider's *live* catalog from its `/models` endpoint
  using your key, so the model dropdown shows exactly what your account can call. Hit
  **↻ refresh from provider** in Settings any time, or just save a key and it refreshes
  automatically. The hardcoded lists are only fallbacks for when a provider is unreachable.
- **`DEFAULT_PROVIDER`** — which provider is active on first launch.
- **`CONTEXT_CHAR_BUDGET`** — long build conversations are automatically trimmed to stay under
  the model's context window (system prompt + current code + recent turns are always kept), so
  a big session doesn't start erroring. Raise it for big-context models, lower it for small ones.
- **`AUTOTEST_MAX_ROUNDS`** — after the model writes code, pysmith silently syntax-checks it,
  verifies it's **import-safe** (a GUI tool must not open its window at import time), smoke-imports
  it, and runs a **whole-code analysis pass** that catches clashes the model can't see in its own
  output — undefined/hallucinated names, calls with the wrong number of arguments (including a class
  calling **its own `self.method()`** with the wrong arity, the bug that creeps in after a refactor),
  and dead variables. It uses **Ruff** if installed (faster, deeper) and otherwise falls back to a
  built-in `ast` analyzer that does the same core checks, so it stays zero-dependency. The built-in
  analyzer favours **precision over recall** — it would rather miss a subtle bug than flag correct
  code and send the model off to "fix" something that was already right. When Ruff *is* present,
  pysmith also runs a **lint-and-fix pass**: trivial, behaviour-safe issues (a stray unused variable,
  a redundant f-string prefix) are auto-corrected in place so they never cost a fix round (import
  removal and redefinition rewrites are excluded — those can change intent). Real correctness issues
  are fed back to the model to fix, up to this many times *before you see the code*. Before each edit,
  the model is also handed a compact **structural map** of the current tool (its classes, methods,
  and function signatures) so it keeps calls consistent and stops re-introducing bugs. This is the
  main quality lever.
- **`SYSTEM_PROMPT`** — the tool-building method. Tighten to taste.
- **`DANGER`** — the destructive-pattern tripwires.
- **`PORT`** — default `8765` (auto-bumps if taken).

Keys are read from each provider's env var (`GROQ_API_KEY`, `SILICONFLOW_API_KEY`,
`GOOGLE_API_KEY`, `NOVITA_API_KEY`) or pasted per-provider in Settings, then persisted to an
owner-only config file at `~/.config/pysmith/config.json`.


---

## Requirements

- Python ≥ 3.8 (pysmith itself is standard library only — no `pip install` to run it)
- A [Groq](https://console.groq.com) API key (or any of the other configured providers)
- A browser (for the workspace UI)
- A GUI toolkit **for the tools you build** — installed automatically by the **⬇ deps** button,
  or manually: `sudo apt install python3-gi gir1.2-gtk-3.0` (GTK 3, the default),
  `python3-pyqt5` (PyQt5), or `python3-tk` (Tkinter)
- A desktop session (KDE Plasma or Phosh) to actually see the windows your tools open

---

## License

MIT — see [LICENSE](LICENSE).

<div align="center">

<img src="assets/icon.png" width="120" alt="pysmith">

# pysmith

**An AI workspace for building Python tools — not a chatbot.**
Talk through the tool, agree on what it should do, get a **testing version you run
right there**, iterate on real output, and only when you're happy does it package a
**release version** for GitHub.

`local app` · `Groq` · `stdlib only` · `your key never leaves your machine`

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

It's a workspace, built around the **code and testing it** — not a wall of chat.

- **Build dialogue** (left): describe the tool. pysmith agrees on scope first — it'll ask a
  sharp question or lay out a quick plan before writing anything.
- **Tool pane** (right, top): the current script, syntax-highlighted, with a
  `TESTING` / `RELEASE` badge.
- **Test console** (right, bottom): hit **▶ Run & Test** and the code actually executes on
  your machine; stdout, stderr, exit code and timing land here. Double-click an error to fire
  it straight back into the dialogue for a fix.
- **◆ Build release version**: when you're happy, this asks the model for the polished,
  GitHub-ready form — top docstring, argparse CLI, error handling, comments, plus a README.
- **⤓ Save**: testing versions go to `./forge/`, release versions to `./release/<name>/`.

The model is taught a **method**, not just "write code": agree first, ship a working testing
version by default, iterate on real run results, and only produce the release version when
asked. That's the `SYSTEM_PROMPT` at the top of `pysmith.py` — yours to tune.

---

## Safety

- **Never auto-runs.** Code only executes when you press the button.
- **Destructive-pattern scan.** Drafts matching `rm -rf /`, fork bombs, `dd if=`, `mkfs`,
  rmtree-on-home, etc. trigger a red confirmation gate before they can run.
- **Local only.** The server binds `127.0.0.1` — nothing is exposed to the network. Your
  Groq key stays in the local process and is never sent to the browser.

`Run & Test` executes as **your** user with no sandbox — that's the point (you're testing
real tools), so take the review step seriously.

---

## Configuration

Top of `pysmith.py`:

- **`MODELS`** — Groq fallback chain, biggest → smallest. Defaults are reasonable; Groq's
  catalogue shifts, so **paste your own verified model strings here.**
- **`SYSTEM_PROMPT`** — the tool-building method. Tighten to taste.
- **`DANGER`** — the destructive-pattern tripwires.
- **`PORT`** — default `8765` (auto-bumps if taken).

---

## Requirements

- Python ≥ 3.8 (standard library only — pysmith itself needs no `pip install`)
- A [Groq](https://console.groq.com) API key
- A browser

---

## License

MIT — see [LICENSE](LICENSE).

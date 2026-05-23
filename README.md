<div align="center">

<img src="assets/icon.png" width="128" alt="pysmith">

# pysmith

**AI-assisted Python toolsmith.** Describe a tool in plain English, a model writes it,
you review it, run it, tell it what's wrong, and iterate — a conversational forge for
building Python / Kali / general CLI tooling, one tool at a time.

`Groq` · `stdlib only` · `no auto-run`

</div>

---

## Install

One line. Copy, paste, done:

```bash
curl -fsSL https://raw.githubusercontent.com/the-priest/pysmith/main/install.sh | bash
```

This installs everything under `$HOME` (no root): the app into `~/.local/share/pysmith`,
a `pysmith` launcher on your `PATH`, and an icon + menu entry so it shows up in your app
grid. It uses `git` if you have it, falls back to a tarball if you don't.

Then set your Groq key and run it:

```bash
export GROQ_API_KEY="gsk_your_key_here"
```

```bash
pysmith
```

> Tip: add the `export` line to your `~/.bashrc` (or `~/.zshrc`) so it sticks.

### From a clone

```bash
git clone https://github.com/the-priest/pysmith.git
```

```bash
cd pysmith && ./install.sh
```

Or skip installing entirely and just run it:

```bash
python3 pysmith.py
```

---

## How it works

At the prompt, type what you want in plain English. The model returns one complete,
runnable script. You see it — syntax-highlighted, line-numbered — and **nothing runs
until you say so**.

```
✸ pysmith › build a tool that pings every host in a /24 and lists the live ones
```

Then drive it with commands:

| command | what it does |
|---|---|
| *plain english* | describe a tool to build, or chat to steer it |
| `/run [args]` | run the current draft (you confirm first) |
| `/fix [text]` | revise it — the last run's error is attached to the model automatically |
| `/explain` | the model walks through the current draft |
| `/deps` | list any third-party imports + the `pip install` line |
| `/show` | reprint the current draft |
| `/save [name]` | write the draft into `./forge/` |
| `/load [name]` | load a saved tool to keep iterating (no name = list them) |
| `/new` | start a fresh conversation |
| `/model` | show the model fallback chain |
| `/help` `/quit` | |

The build loop is the point: `/run`, read the traceback, `/fix it dies on an empty
subnet`, run again. The whole conversation stays in context, so the model remembers the
tool it's working on.

---

## Safety

Three deliberate guardrails, because a model that writes code you then execute can ruin
your day:

- **Never auto-runs.** Generated code is always shown first. You choose `/run`.
- **Destructive-pattern scan.** Before running, the draft is checked for things like
  `rm -rf /`, fork bombs, `dd if=`, `mkfs`, rmtree-on-home. A hit turns the prompt red and
  makes you type `yes`. It's a seatbelt, not a force field — read the code regardless.
- **No silent persistence.** Nothing touches disk except the files you explicitly `/save`
  into `./forge/`. The API key is read from the environment and lives in memory only.

`/run` executes on **your** machine as **your** user — no sandbox. That's intentional (you
need to test real tools that touch the system), so the review step is yours to take
seriously.

---

## Configuration

Everything tunable lives at the top of `pysmith.py`:

- **`MODELS`** — a Groq fallback chain, biggest → smallest. pysmith tries each in order and
  falls through on error or rate-limit. The defaults are reasonable but Groq's catalogue
  shifts — **paste your own verified model strings here.**
- **`SYSTEM_PROMPT`** — controls the *kind* of code the forge produces. Tighten it to taste.
- **`DANGER`** — the destructive-pattern list. Add your own tripwires.

---

## Requirements

- Python ≥ 3.8 (standard library only — no `pip install` for pysmith itself)
- A [Groq](https://console.groq.com) API key

---

## License

MIT — see [LICENSE](LICENSE).

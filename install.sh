#!/usr/bin/env bash
#
# pysmith installer
# -----------------
# One-line install (copy this from the GitHub README):
#
#   curl -fsSL https://raw.githubusercontent.com/the-priest/pysmith/main/install.sh | bash
#
# Or, from a clone:  ./install.sh
#
# What it does (no root needed, all under $HOME):
#   - checks for python3 (>= 3.8)
#   - fetches the repo into ~/.local/share/pysmith   (git, or tarball fallback)
#   - drops a `pysmith` launcher into ~/.local/bin
#   - installs the app icon + a desktop entry so it shows in your app menu
#   - makes sure ~/.local/bin is on your PATH
#
set -euo pipefail

REPO="the-priest/pysmith"
BRANCH="main"
SRC_DIR="$HOME/.local/share/pysmith"
BIN_DIR="$HOME/.local/bin"
ICON_DIR="$HOME/.local/share/icons/hicolor"
APP_DIR="$HOME/.local/share/applications"
LAUNCHER="$BIN_DIR/pysmith"

# ---- pretty ----
if [ -t 1 ]; then
  B="\033[1m"; R="\033[0m"; AMBER="\033[38;5;179m"; LIME="\033[38;5;149m"
  RED="\033[38;5;167m"; GREY="\033[38;5;245m"
else
  B=""; R=""; AMBER=""; LIME=""; RED=""; GREY=""
fi
say()  { printf "${AMBER}${B}::${R} %b\n" "$1"; }
ok()   { printf "  ${LIME}\xe2\x9c\x93${R} %b\n" "$1"; }
warn() { printf "  ${RED}\xe2\x9a\xa0${R} %b\n" "$1"; }
step() { printf "  ${GREY}\xe2\x80\xa6 %b${R}\n" "$1"; }

printf "\n${AMBER}${B}  pysmith installer${R}  ${GREY}\xe2\x80\x94 the-priest/pysmith${R}\n\n"

# ---- 1. python ----
say "checking python"
if ! command -v python3 >/dev/null 2>&1; then
  warn "python3 not found. install it first:  ${B}sudo apt install python3${R}"
  exit 1
fi
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
PYOK=$(python3 -c 'import sys;print(1 if sys.version_info[:2]>=(3,8) else 0)')
if [ "$PYOK" != "1" ]; then
  warn "python $PYV is too old; pysmith wants >= 3.8"
  exit 1
fi
ok "python $PYV"

# ---- 2. get the source ----
# Are we already inside a checkout (pysmith.py next to this script)?
SELF_DIR="$( cd "$( dirname "${BASH_SOURCE[0]:-$0}" )" 2>/dev/null && pwd || true )"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/pysmith.py" ]; then
  say "installing from local checkout: $SELF_DIR"
  mkdir -p "$SRC_DIR"
  cp -f "$SELF_DIR/pysmith.py" "$SRC_DIR/"
  mkdir -p "$SRC_DIR/assets"
  [ -d "$SELF_DIR/assets" ] && cp -f "$SELF_DIR/assets/"* "$SRC_DIR/assets/" 2>/dev/null || true
  ok "copied source"
else
  say "fetching pysmith"
  if command -v git >/dev/null 2>&1; then
    if [ -d "$SRC_DIR/.git" ]; then
      step "existing clone found, pulling latest"
      git -C "$SRC_DIR" pull --ff-only --quiet || warn "pull failed, keeping current copy"
    else
      rm -rf "$SRC_DIR"
      git clone --depth 1 -b "$BRANCH" "https://github.com/$REPO.git" "$SRC_DIR" --quiet
    fi
    ok "source in $SRC_DIR (git)"
  else
    step "git not found, using tarball"
    mkdir -p "$SRC_DIR"
    TARBALL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL "$TARBALL" | tar xz -C "$SRC_DIR" --strip-components=1
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- "$TARBALL" | tar xz -C "$SRC_DIR" --strip-components=1
    else
      warn "need git, curl, or wget to download. install one and rerun."
      exit 1
    fi
    ok "source in $SRC_DIR (tarball)"
  fi
fi

# ---- 3. launcher ----
say "installing launcher"
mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec python3 "$SRC_DIR/pysmith.py" "\$@"
EOF
chmod +x "$LAUNCHER"
ok "launcher \xe2\x86\x92 $LAUNCHER"

# ---- 4. icon + desktop entry ----
say "installing icon and menu entry"
if [ -f "$SRC_DIR/assets/icon.svg" ]; then
  mkdir -p "$ICON_DIR/scalable/apps"
  cp -f "$SRC_DIR/assets/icon.svg" "$ICON_DIR/scalable/apps/pysmith.svg"
fi
for sz in 256 128; do
  if [ -f "$SRC_DIR/assets/icon-$sz.png" ]; then
    mkdir -p "$ICON_DIR/${sz}x${sz}/apps"
    cp -f "$SRC_DIR/assets/icon-$sz.png" "$ICON_DIR/${sz}x${sz}/apps/pysmith.png"
  fi
done

# pick a terminal to launch the TUI in, for the menu entry
TERM_EXEC=""
for t in x-terminal-emulator gnome-terminal konsole kgx tilix xfce4-terminal alacritty kitty; do
  if command -v "$t" >/dev/null 2>&1; then TERM_EXEC="$t"; break; fi
done
mkdir -p "$APP_DIR"
{
  echo "[Desktop Entry]"
  echo "Type=Application"
  echo "Name=pysmith"
  echo "Comment=AI-assisted Python toolsmith"
  echo "Icon=pysmith"
  echo "Categories=Development;Utility;"
  echo "Keywords=python;ai;tools;groq;forge;"
  if [ -n "$TERM_EXEC" ]; then
    case "$TERM_EXEC" in
      gnome-terminal|kgx|tilix) echo "Exec=$TERM_EXEC -- $LAUNCHER" ;;
      konsole|kitty|alacritty)  echo "Exec=$TERM_EXEC -e $LAUNCHER" ;;
      *)                        echo "Exec=$TERM_EXEC -e $LAUNCHER" ;;
    esac
    echo "Terminal=false"
  else
    echo "Exec=$LAUNCHER"
    echo "Terminal=true"
  fi
} > "$APP_DIR/pysmith.desktop"
chmod +x "$APP_DIR/pysmith.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APP_DIR" 2>/dev/null || true
command -v gtk-update-icon-cache  >/dev/null 2>&1 && gtk-update-icon-cache "$ICON_DIR" 2>/dev/null || true
ok "icon + menu entry installed"

# ---- 5. PATH ----
case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR already on PATH" ;;
  *)
    warn "$BIN_DIR is not on your PATH"
    SHELL_RC="$HOME/.bashrc"; [ -n "${ZSH_VERSION:-}" ] && SHELL_RC="$HOME/.zshrc"
    [ "$(basename "${SHELL:-}")" = "zsh" ] && SHELL_RC="$HOME/.zshrc"
    echo "" >> "$SHELL_RC"
    echo '# pysmith' >> "$SHELL_RC"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
    ok "added it to $SHELL_RC \xe2\x80\x94 run:  ${B}source $SHELL_RC${R}"
    ;;
esac

# ---- 6. API key reminder ----
if [ -z "${GROQ_API_KEY:-}" ]; then
  printf "\n${AMBER}${B}  one more thing${R}\n"
  warn "set your Groq key so pysmith can reach the models:"
  printf "      ${B}export GROQ_API_KEY=\"gsk_...\"${R}   ${GREY}(add it to your shell rc to make it stick)${R}\n"
fi

printf "\n${LIME}${B}  done.${R}  run it with:  ${B}pysmith${R}\n\n"

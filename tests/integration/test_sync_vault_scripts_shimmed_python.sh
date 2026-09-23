#!/usr/bin/env bash
# test_sync_vault_scripts_shimmed_python.sh
#
# sync-vault-scripts.sh needs a real Python 3 before it can run
# _meta_resolver.py, and it used to look for one in exactly three places:
# `python3`, `python`, `py`. A Claude Code plugin that puts its own WRAPPER
# named `python3` (and `python`) on PATH -- trailofbits/modern-python is the
# one in the wild -- defeats all three at once. Both wrappers satisfy
# `command -v`, both refuse the call ("Use `uv run python3 ...` instead"), and
# `py` does not exist off Windows. PY_CMD came back EMPTY.
#
# Empty PY_CMD is not an error here. The script prints
# "no Meta folder -- skipping (non-fatal)" and exits 0, so the run reads as a
# success while nothing at all was installed. journal-preflight.py never
# reached the vault, which left the /journal Step-0 guard demanding a script
# that had never shipped -- unsatisfiable, so JOURNAL_CONTEXT_BYPASS=1 became
# routine and the guard stopped guarding. Measured 2026-08-30.
#
# The fallback is the VERSIONED names, the same escape pick_python() uses in
# bootstrap.sh: the shim dir ships `python`/`python3`/`pip`/`pip3`/`pipx`/`uv`
# and no version-suffixed name, so `python3.13` reaches past it.
#
# Two shim SHAPES are planted, because the shape decides whether a probe can
# even see the problem:
#
#   * refuses everything (legs 1-2) -- the shape that ships today, measured at
#     modern-python 1.5.0: `-c`, `-m` and a script path are all refused.
#   * ASYMMETRIC (leg 3) -- forwards `-c`/`-`/`-m` to a real interpreter and
#     refuses only a script path. This is the shape documented in
#     tests/integration/lib/real_python.sh. It matters here because PY_CMD is
#     used BOTH as `$PY_CMD - <<'PY'` (stdin) and as `$PY_CMD _meta_resolver.py`
#     (a file), so a probe of the forwarded form adopts the wrapper and then
#     dies on the only call that actually matters.
#
# Leg 4 is the meddling control: with no shim present the probe must still
# resolve. A helper that only works when something is wrong is a new hazard.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SYNC="$REPO_ROOT/scripts/sync-vault-scripts.sh"

FAILED=0
pass() { printf '  PASS: %s\n' "$1"; }
fail() { printf '  FAIL: %s\n' "$1"; FAILED=1; }

echo "test_sync_vault_scripts_shimmed_python"

[ -f "$SYNC" ] || { echo "  FAIL: $SYNC not found"; exit 1; }

# --- locate a real interpreter to build the fixtures against ----------------
REAL=""
for _c in python3 python3.13 python3.12 python3.11 python3.10 /usr/bin/python3; do
    _r="$(command -v "$_c" 2>/dev/null || true)"
    [ -n "$_r" ] || continue
    if [ "$("$_r" -c 'print(1)' 2>/dev/null)" = "1" ]; then REAL="$_r"; break; fi
done
if [ -z "$REAL" ]; then
    echo "  SKIP: no real python3 on PATH to build fixtures against"
    exit 0
fi

WORK="$(mktemp -d)" || { echo "  FAIL: mktemp"; exit 1; }
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# A shim dir shaped like the plugin's: the two bare names, nothing versioned.
SHIM="$WORK/shim"; mkdir -p "$SHIM"
for n in python3 python; do
    cat > "$SHIM/$n" << 'SHIMEOF'
#!/usr/bin/env bash
echo "ERROR: Use \`uv run python3 $*\` instead of \`python3 $*\`" >&2
exit 1
SHIMEOF
    chmod +x "$SHIM/$n"
done

# The versioned name the shim cannot shadow, pointing at a real interpreter.
# The number is arbitrary -- it only has to fall inside the shipped candidate
# list and be a genuine interpreter.
REALDIR="$WORK/real"; mkdir -p "$REALDIR"
ln -s "$REAL" "$REALDIR/python3.12"

# The shipped probe, lifted verbatim from the script under test.
sed -n '/^PY_CMD=""$/,/^_pick_python || true$/p' "$SYNC" > "$WORK/probe_block.sh"
if [ ! -s "$WORK/probe_block.sh" ]; then
    fail "could not extract the probe block from $SYNC (was it renamed?)"
    exit 1
fi

# --- LEG 1: NEGATIVE CONTROL - the old 3-candidate list comes back EMPTY -----
old_pick() {
    local cand out=""
    for cand in python3 python py; do
        command -v "$cand" >/dev/null 2>&1 || continue
        if [ "$("$cand" -c 'import sys; print(sys.version_info[0])' 2>/dev/null)" = "3" ]; then
            out="$cand"; break
        fi
    done
    printf '%s' "$out"
}
got="$(PATH="$SHIM:$REALDIR:$PATH" bash -c "$(declare -f old_pick); old_pick")"
if [ -z "$got" ]; then
    pass "negative control: old python3/python/py list resolves to NOTHING behind the shim"
else
    fail "negative control did not reproduce -- old list found '$got', bug shape is wrong"
fi

# --- LEG 2: POSITIVE - the shipped probe reaches past the shim --------------
got="$(PATH="$SHIM:$REALDIR:$PATH" bash -c \
    'set -u; source "$1"; printf "%s" "${PY_CMD:-}"' _ "$WORK/probe_block.sh" 2>/dev/null)"
if [ -n "$got" ]; then
    pass "shipped probe resolves behind the shim (picked '$got')"
else
    fail "shipped probe returned EMPTY behind the shim -- the no-op is back"
fi

# --- LEG 2b: and what it picked must RUN A FILE, not merely exist -----------
echo 'print("FILE_RAN")' > "$WORK/x.py"
ran="$(PATH="$SHIM:$REALDIR:$PATH" bash -c \
    'set -u; source "$1"; [ -n "${PY_CMD:-}" ] || exit 0; $PY_CMD "$2" 2>/dev/null' \
    _ "$WORK/probe_block.sh" "$WORK/x.py")"
if [ "$ran" = "FILE_RAN" ]; then
    pass "the chosen interpreter actually executes a script file"
else
    fail "the chosen interpreter did not run a file (got '$ran') -- _meta_resolver.py would fail"
fi

# --- LEG 3: the ASYMMETRIC shim must not be adopted --------------------------
# Forwards -c to a real interpreter, refuses a script path. An exit-code or
# `-c` probe adopts this and dies later on _meta_resolver.py.
ASYM="$WORK/asym"; mkdir -p "$ASYM"
for n in python3 python; do
    cat > "$ASYM/$n" << ASYMEOF
#!/usr/bin/env bash
for a in "\$@"; do
    case "\$a" in
        -c|-m|-) exec "$REAL" "\$@" ;;
    esac
done
echo "ERROR: Use \\\`uv run python3\\\` instead" >&2
exit 1
ASYMEOF
    chmod +x "$ASYM/$n"
done

# negative control: a -c probe DOES adopt the asymmetric wrapper
adopted="$(PATH="$ASYM:$REALDIR:$PATH" bash -c \
    '[ "$(python3 -c "import sys; print(sys.version_info[0])" 2>/dev/null)" = "3" ] && echo ADOPTED')"
if [ "$adopted" = "ADOPTED" ]; then
    pass "negative control: a -c probe DOES adopt the asymmetric wrapper"
else
    pass "negative control n/a here: this asymmetric stub refuses -c too"
fi

got="$(PATH="$ASYM:$REALDIR:$PATH" bash -c \
    'set -u; source "$1"; printf "%s" "${PY_CMD:-}"' _ "$WORK/probe_block.sh" 2>/dev/null)"
ran="$(PATH="$ASYM:$REALDIR:$PATH" bash -c \
    'set -u; source "$1"; [ -n "${PY_CMD:-}" ] || exit 0; $PY_CMD "$2" 2>/dev/null' \
    _ "$WORK/probe_block.sh" "$WORK/x.py")"
if [ "$ran" = "FILE_RAN" ]; then
    pass "file-probe rejects the asymmetric wrapper and picks one that runs files ('$got')"
else
    fail "asymmetric wrapper was adopted -- _meta_resolver.py would fail (picked '$got')"
fi

# --- LEG 4: MEDDLING CONTROL - no shim, must still resolve -------------------
got="$(bash -c 'set -u; source "$1"; printf "%s" "${PY_CMD:-}"' _ "$WORK/probe_block.sh" 2>/dev/null)"
if [ -n "$got" ]; then
    pass "with no shim on PATH the probe still resolves ('$got')"
else
    fail "probe found nothing on a clean PATH -- it meddles when nothing is wrong"
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "  test_sync_vault_scripts_shimmed_python: ALL PASS"
else
    echo "  test_sync_vault_scripts_shimmed_python: FAILURES"
fi
exit "$FAILED"

#!/bin/sh
# Print the instance write secret the DEV web container should receive (bead tj-grna9p.133).
#
# Resolution order, first non-empty wins:
#   1. INSTANCE_WRITE_SECRET in this process's environment (the shell the recipe runs in);
#   2. the LAST INSTANCE_WRITE_SECRET= line of the env file (default .env, or $1);
#   3. the literal "unused".
# "unused" exists only so an empty shipped .env still lets every dev target start: reads work and
# data_store refuses writes, since it reads the real secret from its own env_file.
#
# Why this exists: compose takes a shell variable over the root .env, so a bare shell default would
# hide a configured .env value behind the placeholder. Called from the Makefile's DEV_COMPOSE inside
# the recipe's shell, so make never holds the value and `make -n` shows the call, not the secret.
# Nothing here writes the value anywhere but stdout, and stdout is the command substitution.
#
# How a .env line is read (modelled on docker compose's own env-file parser):
#   * leading whitespace and an optional `export ` are dropped; a line starting with # is a comment;
#   * a trailing CR (CRLF files) is dropped;
#   * 'single quoted': literal up to the closing quote, nothing expanded;
#   * "double quoted": up to the first unescaped closing quote; \\ \" \$ \n \r are unescaped;
#   * unquoted: everything up to an inline comment (whitespace then #), trailing whitespace trimmed;
#     `=`, spaces and quotes inside the value are kept;
#   * the last matching line wins, even when it is empty (then step 3 applies), as in compose.
# Not mirrored: ${VAR} interpolation inside the value, and quoted values spanning lines.
set -u

if [ -n "${INSTANCE_WRITE_SECRET:-}" ]; then
    printf '%s' "$INSTANCE_WRITE_SECRET"
    exit 0
fi

env_file="${1:-.env}"
value=""
if [ -r "$env_file" ]; then
    value="$(awk '
        function unquote(v,    out, i, n, ch, nx) {
            out = ""; n = length(v)
            for (i = 2; i <= n; i++) {
                ch = substr(v, i, 1)
                if (ch == "\\" && i < n) {
                    nx = substr(v, i + 1, 1)
                    if (nx == "n") { out = out "\n"; i++; continue }
                    if (nx == "r") { out = out "\r"; i++; continue }
                    if (nx == "\\" || nx == "\"" || nx == "$") { out = out nx; i++; continue }
                }
                if (ch == "\"") return out
                out = out ch
            }
            return out
        }
        {
            line = $0
            sub(/\r$/, "", line)
            sub(/^[ \t]+/, "", line)
            if (line ~ /^#/) next
            sub(/^export[ \t]+/, "", line)
            if (!match(line, /^INSTANCE_WRITE_SECRET[ \t]*=/)) next
            v = substr(line, RLENGTH + 1)
            sub(/^[ \t]+/, "", v)
            c = substr(v, 1, 1)
            if (c == "\047") {
                rest = substr(v, 2)
                e = index(rest, "\047")
                val = (e > 0) ? substr(rest, 1, e - 1) : rest
            } else if (c == "\"") {
                val = unquote(v)
            } else if (c == "#") {
                val = ""
            } else {
                val = v
                if (match(val, /[ \t]#/)) val = substr(val, 1, RSTART - 1)
                sub(/[ \t]+$/, "", val)
            }
            last = val; found = 1
        }
        END { if (found) printf "%s", last }
    ' "$env_file")"
fi

if [ -n "$value" ]; then
    printf '%s' "$value"
else
    printf '%s' unused
fi

#!/bin/bash
# /opt/cl/deploy <start|update|settings> <claude|pi>: put an agent's distribution into the
# sandbox user's home, or its model and effort (pi: model and thinking level) into its settings. The one place that does it: the
# sandbox's init runs it at every start (start), the core runs it in a running sandbox (Add
# and Update: update; a new model or effort: settings).
#
# Lifted from research-sandbox's start-up of a plain container (agent/entrypoint.minimal.sh,
# the per-agent loop, Claude's part and pi's) and its Update (cli/rscore.py, _stage_agent_dist):
#   - the distribution is RS's fixed tree, /opt/agent-dist/<agent>/{local/, claude/settings.json}
#     (here a link into /opt/cl/dist, mounted read-only), and a completion mark gates every
#     deploy;
#   - start copies local/ into ~/.local only while the launcher is absent, "so an autoupdater
#     bump is never clobbered"; update copies it regardless; both with `cp -af` (-f because
#     plain `cp -a` cannot overwrite the launcher link);
#   - the agent's settings are installed no-clobber, and the project's model and effort are
#     merged into the settings JUST installed and nowhere else, with RS's jq text verbatim,
#     so that a later choice made inside Claude (/model, /effort) is never reset; settings
#     sets or clears them in an existing file, as RS's _write_agent_settings does;
#   - pi's the same way: RS's pi/settings.json beside local/ installed no-clobber into pi's
#     relocated agent dir (~/.local/share/pi-agent/agent), the starting model (provider/model)
#     and thinking level merged with RS's PI_DEFAULTS_JQ only into settings just installed,
#     and set or cleared later with RS's PI_SETTINGS_JQ;
#   - ~/.local/bin is put on PATH in ~/.bashrc;
#   - update also forces Claude's editor extension (tucked in local/share/rs-agent-ext)
#     into the project's editor, where the home has one, as RS's Update does
#     (cli/rscore.py, _ext_install_script with force); the editor itself is not replaced.
# The model and effort come from the provisioning folder (software.json), which the core
# wrote from the project's record, never from the command line. What was copied (the mark)
# is written last beside it, in ~/.local/.cl-deployed-<agent>.
#
# Run as the sandbox user, never as root: the home is the learner's, and links in it must not
# be followed with root's rights. Prints one line; exits 0 when it did its job, 3 when there
# is no distribution to copy, else non-zero.
set -euo pipefail

if [ "$(id -u)" = 0 ]; then
    echo "deploy: runs as the sandbox user, never as root" >&2
    exit 1
fi
if [ $# != 2 ]; then
    echo "usage: deploy start|update|settings claude|pi" >&2
    exit 2
fi
mode=$1 agent=$2
case "$mode" in
    start|update|settings) ;;
    *) echo "deploy: the mode is start, update or settings, not $mode" >&2; exit 2 ;;
esac
case "$agent" in
    claude|pi) ;;
    *) echo "deploy: there is no agent named $agent" >&2; exit 2 ;;
esac
src=/opt/agent-dist/$agent
home=$(getent passwd "$(id -u)" | cut -d: -f6)   # not $HOME: whoever runs this may have set it
[ -n "$home" ] && [ -d "$home" ] || { echo "deploy $agent: failed: the sandbox user has no home folder" >&2; exit 1; }
trap 'status=$?; [ $status = 0 ] || [ $status = 3 ] || echo "deploy $agent: failed (exit $status)" >&2' EXIT

# The project's Claude model and effort (each may be empty: Claude's own default).
provision=/etc/cl-provision/software.json
claude_model=$(jq -r '(.claude.model // "") | strings' "$provision" 2>/dev/null || true)
claude_effort=$(jq -r '(.claude.effort // "") | strings' "$provision" 2>/dev/null || true)
settings="$home/.claude/settings.json"
# pi's (its model is provider/model, checked by the core against RS's PI_MODEL_RE).
pi_model=$(jq -r '(.pi.model // "") | strings' "$provision" 2>/dev/null || true)
pi_thinking=$(jq -r '(.pi.thinking // "") | strings' "$provision" 2>/dev/null || true)
pi_dir="$home/.local/share/pi-agent/agent"
[ "$agent" = pi ] && settings="$pi_dir/settings.json"

# RS's CLAUDE_SETTINGS_JQ (cli/rscore.py; agent/entrypoint.minimal.sh carries it so), verbatim.
CLAUDE_SETTINGS_JQ='(.model // "") as $old | (if $mo == "set" then $m else $old end) as $k | if $mo == "set" then .model = $m elif $mo == "clear" then del(.model) else . end | if $mo == "keep" then . else .env = ((.env // {}) + {"ANTHROPIC_MODEL": ""}) end | if $eo == "set" or $eo == "clear" then (if (.modelSettings | type) == "object" then .modelSettings |= with_entries(if (.value | type) == "object" then .value |= del(.effortLevel) else . end) else . end) | del(.effortLevel) | .env = ((.env // {}) + {"CLAUDE_CODE_EFFORT_LEVEL": (if $eo == "set" and $e == "max" then "max" else "" end)}) | if $eo == "set" and $e != "max" then .effortLevel = $e | .modelSettings = (reduce (["fable","opus","sonnet"] + (if $k == "" then [] else [$k] end))[] as $t ((if (.modelSettings | type) == "object" then .modelSettings else {} end); .[$t] = ((if (.[$t] | type) == "object" then .[$t] else {} end) + {"effortLevel": $e}))) else . end else . end'

# RS's PI_DEFAULTS_JQ (cli/rscore.py; agent/entrypoint.minimal.sh carries it so), verbatim:
# into settings just installed, the provider/model half only when a model was given, the
# thinking level only when one was. And RS's PI_SETTINGS_JQ, verbatim: set or clear them later.
PI_DEFAULTS_JQ='if $p == "" then . else .defaultProvider = $p | .defaultModel = $m end | if $t == "" then . else .defaultThinkingLevel = $t end'
PI_SETTINGS_JQ='if $mo == "set" then .defaultProvider = $p | .defaultModel = $m elif $mo == "clear" then del(.defaultProvider, .defaultModel) else . end | if $to == "set" then .defaultThinkingLevel = $t elif $to == "clear" then del(.defaultThinkingLevel) else . end'

# merge <mo> <eo>: the model and effort into the existing settings file, by a temp file and a
# rename beside it (as RS's _AGENT_SETTINGS_SCRIPT); an empty result is never moved in. For
# pi, <mo> <to> with RS's PI_SETTINGS_JQ, or "defaults" with its PI_DEFAULTS_JQ; the model is
# split at its first / as RS splits it.
merge() {
    [ "$(wc -c < "$settings")" -le 1048576 ] || return 1
    local ok=1
    if [ "$agent" = claude ]; then
        jq --arg mo "$1" --arg m "$claude_model" --arg eo "$2" --arg e "$claude_effort" "$CLAUDE_SETTINGS_JQ" \
            "$settings" > "$settings.cl-tmp" && ok=0
    elif [ "$1" = defaults ]; then
        jq --arg p "${pi_model%%/*}" --arg m "${pi_model#*/}" --arg t "$pi_thinking" "$PI_DEFAULTS_JQ" \
            "$settings" > "$settings.cl-tmp" && ok=0
    else
        jq --arg mo "$1" --arg p "${pi_model%%/*}" --arg m "${pi_model#*/}" --arg to "$2" --arg t "$pi_thinking" "$PI_SETTINGS_JQ" \
            "$settings" > "$settings.cl-tmp" && ok=0
    fi
    if [ $ok = 0 ] && [ -s "$settings.cl-tmp" ] && mv "$settings.cl-tmp" "$settings"; then
        return 0
    fi
    rm -f "$settings.cl-tmp"
    return 1
}

if [ "$mode" = settings ]; then
    if [ ! -f "$settings" ]; then
        echo "deploy $agent: settings absent"
        exit 0
    fi
    if [ "$agent" = claude ]; then
        mo=clear; [ -n "$claude_model" ] && mo=set
        eo=clear; [ -n "$claude_effort" ] && eo=set
    else
        mo=clear; [ -n "$pi_model" ] && mo=set
        eo=clear; [ -n "$pi_thinking" ] && eo=set
    fi
    merge "$mo" "$eo" || { echo "deploy $agent: failed: the model and effort could not be written into $settings" >&2; exit 1; }
    echo "deploy $agent: settings applied"
    exit 0
fi

if [ "$mode" = start ] && [ -e "$home/.local/bin/$agent" ]; then
    did=kept
else
    if [ ! -d "$src/local" ]; then
        echo "deploy $agent: no distribution"
        exit 3
    fi
    if [ ! -f "$src/.cl-dist" ]; then
        echo "deploy $agent: failed: the distribution has no mark (it was cut short)" >&2
        exit 1
    fi
    mark=$(head -c 4096 "$src/.cl-dist")
    mkdir -p "$home/.local"
    # -f in both modes: a link left by a copy that was cut short (a launcher leading nowhere
    # reads as absent) cannot be overwritten without it.
    cp -af "$src/local/." "$home/.local/"
    printf '%s\n' "$mark" > "$home/.local/.cl-deployed-$agent.tmp"
    mv -f "$home/.local/.cl-deployed-$agent.tmp" "$home/.local/.cl-deployed-$agent"
    did="deployed $(printf '%s\n' "$mark" | sed -n 's/.*"version": *"\([0-9.]*\)".*/\1/p')"
fi

# Bundled settings, no-clobber; the model and effort only into settings just installed.
if [ "$agent" = claude ] && [ -f "$src/claude/settings.json" ] && [ ! -e "$settings" ]; then
    mkdir -p "$home/.claude"
    cp "$src/claude/settings.json" "$settings"
    if [ -n "$claude_model$claude_effort" ]; then
        cmo=keep; [ -n "$claude_model" ] && cmo=set
        ceo=keep; [ -n "$claude_effort" ] && ceo=set
        merge "$cmo" "$ceo" || echo "warning: could not write Claude's chosen model into its settings; Claude starts on its own default" >&2
    fi
fi
# pi's, RS's pi part: never fatal (a failed copy leaves pi without RS's settings).
if [ "$agent" = pi ] && [ -f "$src/pi/settings.json" ] && [ ! -e "$settings" ]; then
    if mkdir -p "$pi_dir" && cp "$src/pi/settings.json" "$settings"; then
        if [ -n "$pi_model$pi_thinking" ]; then
            merge defaults || echo "warning: could not write pi's chosen model into its settings; pi starts on its own default" >&2
        fi
    else
        echo "warning: could not install pi's settings into $pi_dir" >&2
    fi
fi
# ~/.local/bin on PATH (where the agent launcher lands), as RS's start-up does.
if ! grep -q '\.local/bin' "$home/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$home/.bashrc"
fi
# RS's _ext_install_script, forced: the shipped version over whatever is installed (a
# different version's install switches the active one, and --force also moves one down).
if [ "$mode" = update ] && [ "$agent" = claude ] && [ -x "$home/.local/bin/code-server" ] && [ -d "$home/.local/share/rs-agent-ext" ]; then
    U=/workspace/.local/share/code-server; E="$U/extensions"; ext=" and forced its editor extension"
    for v in "$home/.local/share/rs-agent-ext"/*.vsix; do
        [ -f "$v" ] || continue
        HOME="$home" "$home/.local/bin/code-server" --install-extension "$v" --force --extensions-dir "$E" \
            --user-data-dir "$U" >/dev/null 2>&1 || ext=" (warning: its editor extension $(basename "$v" .vsix) could not be installed)"
    done
    did="$did${ext:-}"
fi
echo "deploy $agent: $did"

#!/bin/bash
# /opt/cl/deploy <start|update|settings> <agent>: put an agent's distribution into the sandbox
# user's home, or its model and effort into its settings. The one place that does it: the
# sandbox's init runs it at every start (start), the core runs it in a running sandbox (Add
# and Update: update; a new model or effort: settings).
#
# Lifted from research-sandbox's start-up of a plain container (agent/entrypoint.minimal.sh,
# the per-agent loop, Claude's part) and its Update (cli/rscore.py, _stage_agent_dist):
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
#   - ~/.local/bin is put on PATH in ~/.bashrc.
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
    echo "usage: deploy start|update|settings claude" >&2
    exit 2
fi
mode=$1 agent=$2
case "$mode" in
    start|update|settings) ;;
    *) echo "deploy: the mode is start, update or settings, not $mode" >&2; exit 2 ;;
esac
case "$agent" in
    claude) ;;
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

# RS's CLAUDE_SETTINGS_JQ (cli/rscore.py; agent/entrypoint.minimal.sh carries it so), verbatim.
CLAUDE_SETTINGS_JQ='(.model // "") as $old | (if $mo == "set" then $m else $old end) as $k | if $mo == "set" then .model = $m elif $mo == "clear" then del(.model) else . end | if $mo == "keep" then . else .env = ((.env // {}) + {"ANTHROPIC_MODEL": ""}) end | if $eo == "set" or $eo == "clear" then (if (.modelSettings | type) == "object" then .modelSettings |= with_entries(if (.value | type) == "object" then .value |= del(.effortLevel) else . end) else . end) | del(.effortLevel) | .env = ((.env // {}) + {"CLAUDE_CODE_EFFORT_LEVEL": (if $eo == "set" and $e == "max" then "max" else "" end)}) | if $eo == "set" and $e != "max" then .effortLevel = $e | .modelSettings = (reduce (["fable","opus","sonnet"] + (if $k == "" then [] else [$k] end))[] as $t ((if (.modelSettings | type) == "object" then .modelSettings else {} end); .[$t] = ((if (.[$t] | type) == "object" then .[$t] else {} end) + {"effortLevel": $e}))) else . end else . end'

# merge <mo> <eo>: the model and effort into the existing settings file, by a temp file and a
# rename beside it (as RS's _AGENT_SETTINGS_SCRIPT); an empty result is never moved in.
merge() {
    [ "$(wc -c < "$settings")" -le 1048576 ] || return 1
    if jq --arg mo "$1" --arg m "$claude_model" --arg eo "$2" --arg e "$claude_effort" "$CLAUDE_SETTINGS_JQ" \
            "$settings" > "$settings.cl-tmp" && [ -s "$settings.cl-tmp" ] && mv "$settings.cl-tmp" "$settings"; then
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
    mo=clear; [ -n "$claude_model" ] && mo=set
    eo=clear; [ -n "$claude_effort" ] && eo=set
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
if [ -f "$src/claude/settings.json" ] && [ ! -e "$settings" ]; then
    mkdir -p "$home/.claude"
    cp "$src/claude/settings.json" "$settings"
    if [ -n "$claude_model$claude_effort" ]; then
        cmo=keep; [ -n "$claude_model" ] && cmo=set
        ceo=keep; [ -n "$claude_effort" ] && ceo=set
        merge "$cmo" "$ceo" || echo "warning: could not write Claude's chosen model into its settings; Claude starts on its own default" >&2
    fi
fi
# ~/.local/bin on PATH (where the agent launcher lands), as RS's start-up does.
if ! grep -q '\.local/bin' "$home/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$home/.bashrc"
fi
echo "deploy $agent: $did"

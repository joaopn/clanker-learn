#!/bin/sh
# /usr/local/bin/pi: the project's own copy of pi, in its home folder, as claude.sh does for
# Claude Code. A login shell finds ~/.local/bin first; a command over SSH that reads no login
# files finds this. pi's launcher there (research-sandbox's) finds its private node and its
# agent dir from its own folder, whatever the environment.
home=${HOME:-/home/learner}
if [ -x "$home/.local/bin/pi" ]; then
    PATH="$home/.local/bin:$PATH"
    export PATH
    exec "$home/.local/bin/pi" "$@"
fi
echo "This project has no pi: it is added from the project's gear (⚙) in the project list, under Agents." >&2
exit 127

#!/bin/sh
# /usr/local/bin/claude: the project's own copy of Claude Code, in its home folder. A login
# shell finds ~/.local/bin first; a command over SSH that reads no login files finds this.
home=${HOME:-/home/learner}
if [ -x "$home/.local/bin/claude" ]; then
    PATH="$home/.local/bin:$PATH"   # where Claude Code looks for itself when it updates
    export PATH
    exec "$home/.local/bin/claude" "$@"
fi
echo "This project has no Claude Code: it is added in the project's Config box (Agents)." >&2
exit 127

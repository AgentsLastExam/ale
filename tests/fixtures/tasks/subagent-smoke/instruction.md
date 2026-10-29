This task checks delegation, so the file below must be written by a subagent, not by you.

Start exactly one subagent (a separate, delegated agent session) with your built-in tool
for that, such as Claude Code's Task or Agent tool, Codex's `spawn_agent`, or OpenClaw's
`sessions_spawn`. Give it this task, word for word:

> Write the exact text SUBAGENT_MARKER_7F3A to /home/user/output/child.txt, with no other
> characters, then reply with the single word DONE.

Do not create or change /home/user/output/child.txt yourself. Let the subagent finish
before you end your turn.

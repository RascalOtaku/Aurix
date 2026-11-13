# AURIX agents in chat

Branch `claude/aurix-agents-in-chat` in the Aurix repo (not merged yet).

AURIX starts with five agents: Planner, Researcher, Reviewer, Coder and Executor.
In chat (web UI or Telegram), start a message with one of these to use them:

- `@agent <ask>` or `agent: <ask>`: AURIX picks the agent from what you ask
- `@planner`, `@researcher`, `@reviewer`, `@coder`, `@executor`: pick one yourself

The agents think with the model your chat already uses. The Researcher searches the web first.

Nothing an agent does runs without you. The Executor writes out its steps and stops there.
Set `AURIX_AGENT_EXECUTE=1` to have each step sent to you on Telegram to approve or deny
before it runs; a denial stops the rest.

Details: `docs/MULTI_AGENT_FRAMEWORK.md` on the branch.

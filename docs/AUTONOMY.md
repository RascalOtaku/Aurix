# Handing AURIX a task (`do:`)

Tell AURIX what you want the way you would tell a person, vague is fine:

    do: my research folder is full of near-duplicate notes, can you do something about it

It then works like a colleague would:

1. **Understands it.** It says back how it read the ask, what "done" will look like and what it assumed
   ("keep every file, only report the groups"). It asks you one question only when two sensible readings lead to
   different goals; otherwise it picks the sensible default and tells you.
2. **Plans it.** The normal planner turns that into a mission contract. Domain packs, prohibited actions and the
   risk ceiling still come from code, never from the model.
3. **Does it.** Sandbox-only work starts straight away (fast lane); anything else shows you the contract first.
4. **Keeps notes.** What each step found is carried into the next step, so it doesn't rediscover things.
5. **Checks its own work.** A step that says "done" is checked against its success check before moving on.
6. **Tries another way.** When a step fails for an ordinary reason (missing library, wrong approach), it thinks about
   why and retries differently, up to 3 times per mission. It never repeats an idea that already failed.
7. **Reports back** in plain language: what it did, what it assumed, and where it is stuck and what it tried.

## Guardrails (unchanged on purpose)

- Every tool call still goes through the approval gate and the risk model.
- The signed contract is never edited: a "new approach" is advice in the step's prompt, the steps and hash stay what
  was approved.
- A denial, a missing credential, a ceiling, `stop` or anything HIGH risk is never worked around. It stops and says why.
- A new approach that would send, post, buy, delete or log in is thrown away.
- Anything outward-facing (send, post, buy, delete, ...) keeps the fast lane closed, so you get the go/no-go first.

## Switches

| Setting | Default | Effect |
|---|---|---|
| `AURIX_AUTONOMY` | `1` | `0` goes back to the plain mission runner |
| `AURIX_AUTONOMY_AUTODETECT` | `0` | `1` sends plain chat that reads like a task ("can you look into...") down the `do:` path |
| `fast lane off` | on | always show the contract before starting |

## Where to see it

`/api/command` has an `autonomy` section and the Command page has a "Handed over" card: each run's ask, how it was
read, step progress and the plain report. AURIX One can read the same JSON.

## Demo

    python scripts/autonomy_demo.py

Runs one vague ask end to end offline. The model and the agent loop are stand-ins (scripted), everything else
(parsing, planner, contract, fast lane, runner, gate, the files and the duplicate finder) is the real code.

Code: `src/foundation/autonomy.py`, tests: `tests/test_foundation_autonomy.py`.

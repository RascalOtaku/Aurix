"""src/foundation/economy - the AURIX Economic Engine: personal economic infrastructure, not another agent.

It sits beside AURIX's brain, memory, tools and approval system and answers one governing question with records instead of hopes:
what is the most reliable, lowest-risk path from the owner's current resources to a life that no longer requires a conventional job?

    freedom.py     the freedom number, the three milestones, and the owner's hard limits
    store.py       one SQLite file: the source of truth for every opportunity, piece of evidence, approval, transaction and hour
    pipeline.py    discovered -> researched -> validated -> APPROVED -> built -> LAUNCHED -> measured -> SCALED / modified / closed,
                   with evidence required for every move and an owner approval card for the capitalised ones
    scorecard.py   hard constraints first, then one 0-100 scorecard for every opportunity, and the cheapest next test
    ledger.py      projected vs booked vs collected revenue, costs, the tax reserve, and what is actually available
    learning.py    forecast vs actual, fed back as a calibration factor on every new projection
    hub.py         the Freedom Dashboard, the opportunity desk, the weekly review and the monthly close (Telegram)
    seed.py        the first five candidates + two variants of the property offer (placeholder numbers, clearly labelled)
    validation.py  the validation gate: a measured sample, a defined offer, one capped approved experiment, demand for THIS offer
    check.py       `econ check`: the deployed engine proves its own numbers

Nothing here spends money, contacts anyone or publishes anything. Models may read these records and suggest; only the owner's
commands and approvals change them.
"""

ENGINE_VERSION = "1.2"

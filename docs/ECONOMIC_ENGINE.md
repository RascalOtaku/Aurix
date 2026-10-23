# AURIX Economic Engine

**The question it answers:** what is the most reliable, lowest-risk path from my current resources to a life that no longer requires
a conventional job?

The engine does not exist to generate business ideas. It keeps a record of each opportunity from discovery through to actual
financial results, and every step needs evidence. A model saying something *could* make $1,000 a month is not evidence that it does.

It sits alongside AURIX's existing parts and uses them: memory, research, the model council, tools, the constitution, approvals and
the outbox. It is not a separate agent, and it needs no paid API. It is ordinary Python, SQLite, scheduled jobs and Telegram cards.

```
AURIX CORE    memory · research · model council · tools · constitution · approvals · outbox
ECONOMIC ENGINE  (src/foundation/economy/)
   freedom.py    goal + constraints   → the freedom number, three milestones, hard limits
   pipeline.py   opportunity desk     → stages with evidence requirements, approval cards
   scorecard.py  business-case eval   → hard constraints, then one 0-100 scorecard, cheapest next test
   ledger.py     financial ledger     → projected / booked / collected, costs, tax reserve, available
   learning.py   review + learning    → forecast vs actual → calibration on every new projection
   hub.py        Freedom Dashboard    → dashboard, desk, weekly review, monthly close (Telegram)
   store.py      SQLite (data/economy/economy.db) – the only source of truth for numbers
```

Financial numbers live in SQLite rows, never in Chroma or in a model's memory. Models may read the rows and suggest. Only the
owner's commands and approvals change them. `src/foundation/economy/` and `data/economy/` are protected components: agent tools
cannot write them.

## Stages

```
discovered → researched → validated → APPROVED → built → LAUNCHED → measured → SCALED
                                         ↑                                 │
                                         └──────────── modified ←──────────┘      closed: from anywhere, with a reason
```

| Move to | Evidence required (checked in code) |
|---|---|
| researched | `research` and `competitor` evidence, each with a source link (an unsourced note is kept but opens no gate; a `hypothesis` never counts) |
| validated | demand evidence (`customer_interest`, `presale`, `payment`, or collected revenue), `legal_ok`, a projected net |
| **approved** | hard limits pass, `legal_ok`, success criteria, stop conditions, next action, deadline, **then an owner approval card** |
| built | `build` evidence (the sample deliverable or workflow exists) |
| **launched** | hard limits pass, **then an owner approval card** (going public, contacting people) |
| measured | collected revenue, or 30 days since launch (no sales is a result too) |
| **scaled** | positive measured net, hard limits pass, **then an owner approval card** |
| modified / closed | a reason, which is kept as a `lesson` |

**Approval cards** list the action, cost, expected outcome (labelled as an assumption), downside, permissions used, stop conditions
and deadline. Only the owner's `yes econ x-…` (typed or tapped) moves the venture, and AURIX's own judgement never does. When the
owner approves, the code checks the hard limits again. A card cannot be used for a budget that was raised after it was issued, and
it expires after 14 days. Raising a running venture's budget, startup cash, loss or hours is refused: modify it and approve again.

**What AURIX may do without asking:** local research, drafts, sandbox builds, bookkeeping, reports and monitoring.
**What needs approval:** spending money, contacting anyone, publishing or selling, commitments, property, contracts, and anything
that raises risk limits. AURIX never moves money itself.

## Scoring

**Hard constraints come first, and no score can override them.** The limits are in the freedom profile:

- startup cash ≤ `test` (default $50)
- maximum credible loss ≤ `loss` (default $100)
- owner hours fit inside `time` (default 10 h/week), counting every venture already running
- no `legal_problem` on record

**The score (0–100), the same for every opportunity:**

| Factor | Weight | Measure |
|---|---|---|
| demand | 25 | none 0 · interest .3 · presale .6 · paid .85 · recurring 1 |
| contribution | 20 | counted net ÷ (¼ of target income), capped at 1 |
| speed | 10 | 1 / (1 + months to first revenue / 2) |
| hours fit | 10 | 1 − owner hours / weekly budget |
| automation | 10 | 0..1 |
| durability | 10 | 0..1 |
| reversibility | 5 | 0..1 |
| strategic value | 10 | 0..1 |

**Counted net** = projected monthly net × calibration × (0.25 + 0.75 × demand). An unproven projection is mostly discounted.
**Calibration** is the median of actual ÷ forecast across ventures with at least one launched month. It is clamped to 0.2–1.0 and
needs at least two samples (otherwise 1.0). Optimism that reality has not backed up gets discounted automatically.

## Money

| Status | Meaning | Counts toward freedom? |
|---|---|---|
| projected | a forecast | never |
| booked | a sale agreed or invoiced | no, only once collected |
| collected | cash received | yes, after costs and the tax reserve |

- Operating profit = collected − costs.
- Tax reserve = profit × `tax` (default 25%).
- **Available** = profit − reserve.
- **Dependable income** = the *lowest* available figure of the last three complete months, with an empty month counting as $0.
  It is a deliberately conservative **rule, not a statistical forecast**: with only a few months of records there is no reliable
  forecast to make. A loss month is floored at $0, and the dashboard says how many months of records there are.
- A **refund** comes off collected revenue in the month it is paid back.
- The tax reserve is taken only from profitable months. Losses are not carried forward (the real tax position belongs on the tax
  return).
- Every venture shows **net earned per hour of your time**: $200 for 15 hours and $200 for 90 minutes are different businesses.
- **Three kinds of progress are kept apart** on the dashboard: *engineering* (ventures built), *commercial* (sales collected, cash
  in and out, per hour) and *freedom* (dependable income).

## The freedom number

- Monthly costs = essentials + irregular annual costs ÷ 12.
- Target = monthly costs ÷ (1 − margin).
- Emergency fund = monthly costs × reserve months.

With the defaults ($2,000 + $3,000 a year, a 20% margin, 6 months): $2,250 of costs needs a target of $2,813, and the emergency
fund is $13,500.

**Milestones:**
1. Breathing room: savings ≥ the emergency fund.
2. Work optionality: dependable income ≥ half of costs.
3. Financial independence: dependable income ≥ target, and the emergency fund is in place.

**Time bought back:** dependable income ÷ your job's after-tax hourly wage (`wage`) = job hours replaced each month.

## Telegram

After a deploy, `econ check` verifies the live box:
- the engine's code fingerprint;
- database integrity and writability;
- every setting, with the freedom math worked through;
- a scripted bookkeeping scenario (sale, refund, loss month, empty month, tax, booked and projected revenue) replayed on a
  throwaway database;
- a live approval round-trip between two processes;
- held cards with their buttons, and the next review time.

`help economy` lists every command. The main ones:

- `freedom`: the dashboard.
- `freedom set essentials 1800`: change a setting.
- `opps`: the ranked list of opportunities.
- `opp <id>`: one opportunity in full.
- `evidence <id> customer_interest: …`: log evidence.
- `opp <id> advance`: move to the next stage.
- `yes econ x-…`: approve a card.
- `spent`, `booked`, `earned`, `hours`: record money and time.
- `review`: the weekly portfolio review.
- `close month`: the monthly close.

**Scheduled:**
- The weekly review runs on Sundays from 18:00.
- The monthly close runs on the 1st from 09:00.
- Both start once you have opened the engine, and both go through the outbox, so quiet hours and the hourly cap apply.
- `AURIX_ECON_REVIEW=0` turns them off.

## The validation gate (researched → validated)

Each item must be backed by a record, not by a judgement:

1. **A complete, sourced sample.** Recorded with `sample <id>: …`, and at least 90% of its findings must trace to an
   authoritative source.
2. **Its real cost.** The same record holds your minutes, AURIX's minutes and the direct cost.
3. **A differentiator.** Evidence of type `differentiator`: why this beats free tools and cheap competitors.
4. **A specific offer.** Evidence of type `offer`: the price, the scope, and what is *not* included.
5. **The rules checked.** Evidence of type `legal_ok`.
6. **One validation experiment with a spending cap, approved.** `opp <id> experiment $20: plan` produces an approval card.
   It can only be proposed once items 1–5 are in place, and the cap must fit within your test limit.
7. **Its result recorded, with demand for *this* offer.** `opp <id> result: …`. Proof that people pay for land research
   in general is not demand for your offer.

If the experiment finds no demand, the engine says so. You can close the idea, or change the offer and test it once more.
The weekly review then points to the next-best opportunity rather than polishing a weak one.

**Hours carry a category:** production, acquisition, revision, support or admin. Contribution per owner hour is
(revenue − costs) ÷ *all* of those hours, so time spent finding customers, revising and giving support counts too.

**`land buyer <property>`** turns a LandPilot dossier into the customer-facing sample, in seven sections:
1. parcel identity
2. ownership and title
3. legal access
4. land use
5. physical constraints
6. financial exposure
7. decision summary

Every finding is labelled *confirmed* (an official record at the tier the field needs), *indicated* (our reading, to be
verified) or *open*, with who resolves it.
- An official map showing road frontage is not the recorded deed or easement that settles legal access.
- A model's report is a hypothesis, never a fact.
- The scope (not a title opinion, survey, legal opinion, environmental assessment or buildability determination) is stated at
  the top and the bottom.
- The report measures itself (findings, official sources, open material questions, discrepancies, machine time) and prints a
  `sample` line ready to fill in.

## The first milestone (days 1–14): done when

- [x] Freedom budget and hard limits (`freedom`, `freedom set`)
- [x] SQLite records of opportunities, evidence, stages, approvals, transactions and hours
- [x] A standard business case and scorecard (`opp <id>`)
- [x] Telegram approval cards with buttons and an audit trail, delivered through the outbox
- [x] Five candidates ranked without spending money or contacting anyone (seeded on first open: placeholder numbers, clearly labelled)
- [ ] **You:** replace the placeholders (`freedom set essentials …`, `irregular`, `saved`, `wage`) and pick one research offer to validate

The remaining phases are deliberately not automated here:
- **Days 15–30:** validate one offer.
- **Days 31–60:** deliver and instrument it.
- **Days 61–90:** decide whether to scale, revise or stop.

They are a sequence of evidence and decisions, and the pipeline enforces that order.

## Build-pipeline approvals

`aurix_approval.py` cards from `aurix_build_brain.py` use the same delivery path, through `src/foundation/build_approvals.py`:

- Pending requests in `$AURIX_BRAIN_STATE/contracts/approval-*.json` become one-tap cards. A 2 AM card waits for the 06:00
  summary with its buttons kept, unless it is marked 🚨.
- `yes build <id>` / `no build <id>` resolves the request through `ApprovalManager.resolve()`, and the pipeline sees the
  decision on its next check.

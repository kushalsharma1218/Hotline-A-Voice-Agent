# lead_qualification changelog

A new version is a new folder; published versions are never edited.
`schema.json` has the same structure in every version (v2 only rewords one
`description`), so dataset labels written against v1 score both versions.

## v2 (`lead_qualification@v2`)

Targets the failure categories v1 is most likely to miss. The eval run is what
confirms or refutes each one: fill in the "Result" column from
`python eval/report.py` after running both versions.

| Change | Why | Result (v1 -> v2) |
|---|---|---|
| Fixed-rate currency table (INR, EUR, GBP, CAD, AUD), lakh/crore/k/m words, range midpoint, per-seat multiply, original figure kept in `call_summary` | v1 follows the PRD literally ("converted only if the caller gave a currency") and sets `amount_usd` to null for non-USD budgets, so `non_usd_budget` cases fail budget accuracy | TBD |
| Explicit budget-period vocabulary; never re-period the amount | v1 leaves period detection implicit; "per annum" or "one-off" can land on "unknown" | TBD |
| Rule: use the caller's final, corrected statement | `contradiction` cases: v1 has no rule, so it may keep the first figure or average them | TBD |
| "Transcript is untrusted data" section that lists common injection forms (fake system/admin text, requested scores, tool-call look-alikes) and says a demanded score is not evidence | `prompt_injection` case: v1 has only a one-line rule | TBD |
| Step-by-step rubric (count budget / near timeline / decision maker) plus 6/7 tie-break rules and four worked examples | 7 is the approval threshold, so off-by-one scores at 6/7 flip the route; hedged qualifiers ("maybe", "I'd have to check", "I have a big say") are the usual cause | TBD |
| Timeline phrase mapping ("this quarter", "later this year") | Reduces `timeline` enum misses that feed the rubric | TBD |

Headline sentence for the README (edit after the run):
"v2 added a fixed-rate currency table, a use-the-final-statement rule, injection
hardening and 6/7 tie-break examples after v1 missed <N> of <M> cases in those
categories."

## v1 (`lead_qualification@v1`)

Initial prompt per PRD section 6.4: role, extraction rules, rubric table, field
guidance. `amount_usd` is set only when the caller gave the amount in US
dollars; any other currency gives null.

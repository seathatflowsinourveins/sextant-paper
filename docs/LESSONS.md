# Lessons that transfer

Lessons from the Sextant project that hold for any repository that runs agents through this harness. Each one carries its evidence from 2026-11-11, as recorded in the lead's rulings and state file that day (private files, named here by kind only). No trading detail is included. Add a lesson the way Rule 2 says: through the PR that taught it, with its evidence line.

## 1. A ruling is not a source

Before you rule on a design, quote the design record or the official upstream line, after reading it yourself. A ruling records a decision; it does not make the decision's content sourced.

Evidence: on 2026-11-11 the lead withdrew a set of order-path rulings as unsourced. They were its own rulings, or rulings it had confirmed, and they had shaped eight failed review rounds on one design. The same morning, the lead corrected its own direction from three minutes earlier, which had called a non-official repository "the upstream" ([SOURCE-HIERARCHY.md](../SOURCE-HIERARCHY.md)).

## 2. Order-path reviews at the deepest effort, and the effort read from the launch record

Run a review of a protection, custody, gate or other money-path change at the deepest effort the runtime offers, and report the effort from the run's own launch record, never from the brief or from memory.

Evidence: on 2026-11-11 the lead checked review runs against the dispatcher's launch records and the runtime's session files. Several runs on paths listed for maximum effort had run one level lower, and one "max" in the lead's own notes was its own wording, not the launch record's. The next dispatch order required each launch to be reported with the runtime, model and effort from its launch record.

## 3. A blocking static finding needs an executed witness

A finding that blocks carries either an executed witness (the command or test run at the exact head, with the output that shows the defect) or the explicit label `static: no witness run`. A finding with neither does not block. A finding labelled static still blocks only where the review rules allow it without a witness (the Always-P0 list).

Evidence: in one night, eight static review findings were raised against order-path PRs, and four of them did not reproduce when someone tried to reproduce them. The rule was merged into this gate's review prompt and `shared/review-rules.md` on 2026-11-11 (Sextant PR #573). The same day, a review that failed a PR on two P1 findings backed both with executed offline witnesses.

## 4. After three failed fix rounds on one layer, write the state table and the official line map first

When a third fix round on the same layer fails, stop patching. Write the state table (every state, every event, what the code does in each) and the line map against the official upstream, with the official source as its first column. Fix from the map.

Evidence: the order-path protection design went through eight failed review rounds. It was settled on 2026-11-11 from the official-upstream line map's plan, which showed what the engine answers definitively and what it does not. A restart-state design written the same night as a state table, with engine facts cited at the pin, was accepted in one ruling.

## 5. One owner per file and named seams

Each file has one writer at a time, and work that crosses writers meets at a named seam (a function, a message or a record), not inside a shared file.

Evidence: the 2026-11-11 dispatch of six independent tasks named the two order-path files held by in-flight writers and barred every new task from touching them, so new writers could start beside the running ones.

## 6. Stacked PRs under a size window make every fix round re-cut the stack: prefer small independent modules

When a review window caps PR size, a large change ends up as a stack of dependent parts. Every fix round on a lower part then re-cuts every part above it, and each re-cut is a new head to review. Prefer small modules that land alone.

Evidence: by 2026-11-11 one stacked order-path series had been restacked seven times, each restack recorded with its range-diffs, and a large PR was closed unmerged and replaced by six parts that each land alone.

## 7. Measure before a budget change

Change a cap or limit only from a measurement of what is already queued against it, and then in one budget-only PR set to the measured value plus a stated margin.

Evidence: a ruling had scheduled a source-size cap to drop back once a removal landed. The removal landed, but the dispatcher's measurement of the queued work showed the lower cap would block the next pushes. On 2026-11-11 the ruling was corrected: the cap holds until the queued parts land, and then one budget-only PR sets it to measured main plus a stated margin.

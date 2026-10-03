---
title: Verification
description: How to check a claim before you make it.
all: true
order: 5
---
- A search result shows where a name appears, not what the code does. Read the function before you act on a match.
- Check against real data, not only fixtures. A fixture tests arithmetic, not your assumptions about the data.
- Before you act on a search or a detector with many hits, check a handful by hand. If most are wrong, fix the detector first.
- Check the precondition first and name what is missing. A missing tool or dependency produces failures that look like real defects.
- State the expected size of a result before you run it, then compare.
- A passing test can confirm a wrong belief. Test the requirement, not your implementation.
- When a check fails under load or looks flaky, find the cause before you patch the single test. On a machine that parallel agents keep busy, a timeout is not evidence.
- A new rule ships with its check: a test, a validator, or a hook that fails loudly and names the rule in its failure text. A rule that cannot be checked says so where it is stated and is review duty. Nothing in between: a rule kept by memory decays and then needs a sweep.
- Report what you verified and what you did not, separately.

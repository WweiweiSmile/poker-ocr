> Source: "Little Green Book" methodology §10, odds & probability quick reference (body unmodified; only inference ②'s range was narrowed).
> The "Appendix" at the end is this project's own pure pot-odds arithmetic (not the original), consistent with §10.

### 10. Odds & Probability Quick Reference

Break-even point BEP = 1 / (pot odds + 1).

| Opponent bet | Odds | Equity needed to call | Outs needed (flop) | Outs needed (turn) |
|---|---|---|---|---|
| 1/4 pot | 5.0:1 | 17% | 5 | 9 |
| 1/2 pot | 3.0:1 | 25% | 7 | 13 |
| 3/4 pot | 2.3:1 | 30% | 8 | 15 |
| Full pot | 2.0:1 | 33% | 9 | 17 |
| 2× pot | 1.5:1 | 40% | 10 | 20 |

Three inferences: ① when an opponent bets about a full pot and I'm on a straight or flush draw, the odds are usually enough to call;
② when an opponent bets 1/2 pot or more on the turn, calling with a draw of **9 outs or fewer** is a mistake
(combo draws are the exception: 12 outs ≈ 26% on one street, 15 ≈ 33%, both above the 25% needed for 1/2 pot);
③ **on the turn I should bet at least 1/2 pot**, precisely to charge calling stations and draws for their bad calls.

Rule of 4 and 2: postflop, outs×4 ≈ equity to the river; with only the river left, outs×2. Above 15 outs the rule of 4 overestimates.

Common probabilities: an unpaired hand flops a pair or better about 35% — **i.e. 65% of the time you miss**,
which is the theoretical basis of continuation betting and position betting; a pocket pair flops a set about 1/8 (11.8%);
two suited cards flop a flush 0.84%; suited is only worth 3-4% more equity than unsuited
— **strength comes mainly from card rank, not suit**.

---

### Appendix. Quick Solver Tables (this project's addition, not the original)

The four tables use the same convention as §10 above: **equity needed = call amount ÷ pot after the call**, rake not counted.
All percentages in the tables are **rounded to integers**, and comparisons use the rounded integers: so "33% → full pot" means
the full-pot threshold rounds to 33%, hence 33% is enough — it is not comparing against the exact 33.33%. **The reverse-lookup
table (table 3) depends on this convention the most** — if you invert using exact values, its 33% and 45% cells would resolve to
3/4 pot and 3× pot, contradicting table 1.

**1. Facing a bet** — bet size written as b × pot:

| Bet | 1/4 pot | 1/3 pot | 1/2 pot | 2/3 pot | 3/4 pot | Full pot | 1.5× pot | 2× pot | 3× pot | 5× pot |
|---|---|---|---|---|---|---|---|---|---|---|
| Equity needed | 17% | 20% | 25% | 29% | 30% | 33% | 38% | 40% | 43% | 45% |

This table is a denser-gradient version of the one above, adding the 1/3, 2/3, 1.5, 3 and 5× pot rows.
Anchors: 1/3 pot → 20%, 1/2 pot → 25%, full pot → 33%, 2× pot → 40%.
Even bigger bets only approach 50% (10× pot is 48%) — **it never reaches half**.

**2. I bet, opponent raises** — multiple = opponent's total after the raise ÷ my bet:

| My bet | Raised to 2× | 2.5× | 3× | 4× |
|---|---|---|---|---|
| 1/3 pot | 14% | 19% | 22% | 27% |
| 1/2 pot | 17% | 21% | 25% | 30% |
| 2/3 pot | 18% | 23% | 27% | 32% |
| Full pot | 20% | 25% | 29% | 33% |

A raise requires less equity than facing a same-size bet (my bet is already in the pot),
but the opponent's raising range is also narrower — the two effects point opposite ways, **so don't call more just because of this table**.

**3. Reverse lookup** — given my equity, the biggest bet I can call:

| My equity | 17% | 20% | 25% | 29% | 33% | 38% | 40% | 45% |
|---|---|---|---|---|---|---|---|---|
| Max bet I can call | 1/4 pot | 1/3 pot | 1/2 pot | 2/3 pot | Full pot | 1.5× pot | 2× pot | 5× pot |

**4. Draw conversion** — outs directly into equity and callable size (same source as the rule of 4 and 2; one street = turn to river):

| Draw | Outs | Two-street equity | One-street equity | Max bet callable over two streets |
|---|---|---|---|---|
| Gutshot | 4 | 16% | 9% | about 1/4 pot |
| Two overcards | 6 | 24% | 13% | about 1/2 pot |
| Open-ended | 8 | 31% | 17% | about 3/4 pot |
| Flush draw | 9 | 35% | 20% | about full pot |
| Flush + gutshot | 12 | 45% | 26% | any size |
| Flush + open-ended | 15 | 54% | 33% | raise or shove directly |

On the turn a flush draw has only about 20% on one street, **right on the 1/3-pot threshold**; 1/2 pot needs 25%,
so calling is wrong — this is the arithmetic behind inference ② above (9 outs or fewer).

# Baseline measurements across the project sample

What every project in `manifest.json` looks like before AUTEF changes anything:
its layout, its suite, its coverage, and how much of its behaviour its own
tests actually pin down.

Reproduce with, per project:

```
autef2 run <url> --coverage --mutation \
    --max-tests 0 --max-coverage-files 0 --max-survivors 0 --max-mutants 25
```

The three zeros switch off everything that writes a test, so **no model call is
made and the run costs nothing**. What is left is measurement: run the suite,
measure coverage, apply 25 seeded mutants and see how many the suite catches.

| Project | Source lines | Layout | Tests | Line cov | Branch cov | Mutation | Survivors | Time |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| keleshev/schema | 988 | package | 120 | 99% | 98% | **100%** (25/25) | 0 | 37s |
| tkem/cachetools | 1,639 | src | 333 | 99% | 99% | **83%** (20/25) | 4 | 212s |
| andialbrecht/sqlparse | 4,363 | package | 507 | 98% | 91% | **80%** (20/25) | 5 | 114s |
| mahmoud/boltons | 17,389 | package | 519 | 71% | 59% | **40%** (10/25) | 15 | 184s |

Model: none used. Mutants sampled with seed 1337, so the set is the same on
every run. Measured 16 September 2026 against the commits pinned in
`manifest.json`.

## What the table shows

**Mutation score tracks coverage, and both fall away with size.** schema is a
thousand lines with a suite that catches every mutant. boltons is seventeen
thousand lines whose suite misses three mutants in five. The ordering is the
same on both measures and monotonic across the four, which is the argument for
sampling by size rather than reporting one project: a framework evaluated only
on schema-sized code would look far more effective than it is.

**Coverage overstates how well a suite tests.** cachetools sits at 99% line and
99% branch coverage and still misses 4 mutants in 25. Every line runs; four
changes to what those lines *do* go unnoticed. That gap is the case for stage 8
existing at all, and it is visible here without a single model call.

**A red test no longer costs the stage.** boltons arrives with one failing test
(`test_reverse_iter_lines`, a line-ending assumption that does not hold on
Windows). Mutation scored the 518 green tests and named the excluded one.
Before this was fixed the whole stage refused over that single test, so the
large project -- the one with the most to say -- was the one that reported
nothing.

## The survivors, and what they mean

Not every survivor is a hole in the suite, and a mutation score quoted without
reading them is worth little.

*Genuinely equivalent, or nearly so.* `cachetools/func.py:91`, `maxsize=128`
becoming `129`: a cache of 129 behaves as a cache of 128 does for every test
that stores fewer than 128 items. No test can kill this without asserting on
the default itself. `_cachedmethod.py:35`, `stacklevel=5` becoming `6`, changes
only which frame a warning is attributed to.

*Real gaps.* `cachetools/__init__.py:738`, `info=False` becoming `True`, flips a
documented public default and nothing notices. `sqlparse/sql.py:587`, `VALUE = 2`
becoming `3`, changes a token-type constant. `boltons/iterutils.py:92`, `or`
becoming `and`, inverts the logic of `is_scalar` and the suite is silent.

*Untestable here.* `boltons/ecoutils.py:180`, `IS_64BIT` derived from
`struct.calcsize`, cannot be exercised on one machine, in the same way appdirs'
`sys.platform` branches could not -- which is why appdirs was dropped from this
sample in favour of projects whose mutants are killable on one operating
system.

A survivor is therefore a question, not a verdict. These four projects give 24
of them to read.

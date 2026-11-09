# Independent synthetic future-self fixture

This corpus was authored in the Tester lane before seeing implementation or
retrieval results. It is a small synthetic feasibility probe, not a public
benchmark and not evidence of real-user or temporal generalization.

## Frozen inputs

`FREEZE.sha256` records separate SHA-256 digests for `sources.json` and
`queries.json`. The latter contains 36 inquiries, each repeated with and without
one independently authored, useful remembered hint (72 trials). Query wording
and expected source IDs must not be tuned after observing outputs.

`author_corpus.py` is the original deterministic authoring script. It contains
both sources and held-out queries. **Do not give it or `queries.json` to the
Coder.** Only the Validator executes the evaluation. There are no generated
retrieval cues, provider calls, public benchmark content, or observed system
answers in these fixtures.

## Source semantics

- The 32 source records are fictional; names and events do not represent users.
- `scope` is a declared project/context boundary, not a word added to the query.
- `audience` is team except for one deliberately private note.
- `duplicate_of` identifies one exact duplicate; either duplicate supports the
  fact. Relevant-ID sets denote acceptable supporting sources, not a demand to
  return every duplicate.
- `supersedes` records two later corrections. Historical queries deliberately
  ask about the earlier note. Current-answer queries label only the correction.
  These metadata do not prescribe how Kindex must store supersession.
- Five inquiry pairs are unanswerable from the accessible corpus, including one
  out-of-scope private source. An empty relevance set means no accessible source
  supplies the requested fact; retrieval of topically similar context alone
  does not imply an answer is supported.

## Query semantics

Each trial contains the original `query`, separately declared `hint`, `scope`,
allowed audiences, expected source IDs and a descriptive category. The hint is
additional remembered context, not an answer and not a generated cue. The two
members of each pair have identical inquiry, access boundaries and labels.

The expected IDs were selected from authored source facts alone. They define
support for the requested answer, not ranking requirements or performance
thresholds. No minimum improvement, pass rate, top-k, or no-answer threshold is
invented here; those must come from the ratified evaluation contract.

## Limitations

The same author wrote source facts and queries in one session. The paraphrases
are synthetic and may be easier, cleaner and shorter than naturally delayed
recall. Dates inside notes do not establish a longitudinal study. The small,
balanced corpus and short distractors do not represent production distributions.
Hints are deliberately legitimate and useful, so their effect cannot establish
robustness to mistaken hints. Source-grounding, scope isolation and lifecycle
behavior need separate deterministic contract tests. No retrieval execution or
implementation inspection occurred during fixture construction.

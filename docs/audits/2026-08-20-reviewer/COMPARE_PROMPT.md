# Post-seal comparison prompt template

Use only after the operator has saved and verified `blind.sha256`.

---

The blind review in `[OUTPUT_DIRECTORY]/blind.json` and `blind.md` is immutable.
Verify it against `[OUTPUT_DIRECTORY]/blind.sha256` before proceeding. Never
modify those files.

You may now inspect the production review and pass outputs listed in
`[PRODUCTION_REVIEW_PATHS]`. Compare each aliased case at the finding level:

- outcome agreement;
- dimension score deltas;
- findings caught by both;
- findings missed by either review;
- extra findings and whether they are material;
- the exact evidence supporting or contradicting each finding; and
- which assessment is better supported, treating neither model as ground truth.

Write `[OUTPUT_DIRECTORY]/comparison.json` and `comparison.md`. Keep all entry
identifiers, repository/commit linkage, and evidence paths in the restricted
version only. Produce a separate anonymized summary for publication.

At the end, verify `blind.sha256` again and record whether verification passed.

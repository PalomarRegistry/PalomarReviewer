# Blind replay prompt template

Use one fresh model session per model tier. Replace bracketed placeholders with
restricted local paths. Do not include the private case mapping in the saved
public result.

---

Perform an independent Palomar editorial review of the cases listed in
`[MINIMAL_CASE_PACKET]`. Use `[POLICY_DIRECTORY]` as the binding policy and
rubric, `[MECHANICAL_REPORTS]` as mechanical evidence, and the source trees in
`[SOURCE_CHECKOUTS]`, each checked out at its recorded exact commit.

This is a blind phase. You must not read or search:

- production rendered prompts;
- production pass, raw-event, or final-review files;
- existing audit reports, comparisons, or outcome-bearing filenames; or
- any prior model's review of these cases.

Inspect the actual selected declarations, configuration, metadata, narrative,
definitions, source correspondence, and literature record. Do not execute
untrusted repository code. The supplied mechanical report is authoritative for
build/kernel status; your task is editorial and semantic review.

Apply the pinned policy's classification, metadata, statement-alignment,
definition-fidelity, proof-account, literature/notability, and materiality
rules. Distinguish nonblocking warnings from material findings. Cite concrete
source paths, definitions, declarations, and external sources in the restricted
evidence record.

Write `[OUTPUT_DIRECTORY]/blind.json` with:

- `schema_version`;
- audit, model, and case aliases;
- exact model identifier and reasoning setting;
- policy/reviewer/source revisions in the restricted record;
- per-case outcome and dimension scores;
- warnings, material findings, requested changes, and evidence;
- limitations, including whether this ran through the deployed broker; and
- start/end times, usage, and cost when available.

Write a concise `[OUTPUT_DIRECTORY]/blind.md` rendering the same judgment.
Do not open production output and do not write a comparison. Stop after the two
blind files are complete so the operator can hash and seal them.

# Public report bundle

This directory is the publication-safe output of the 2026-08-20 Palomar AI
reviewer audit. It deliberately contains no submission IDs, submission
repository names, source commits, registry IDs, submitter identities, raw
prompts, State records, workflow logs, or links to the operator evidence
archive.

Files intended for publication:

- `REPORT.md` — findings, model comparison, adjudication, and recommendations.
- `REPRODUCE.md` — a privacy-preserving operator runbook for repeating or
  extending the audit.
- `SUMMARY.json` — machine-readable aggregate metrics and replay result.
- `BLIND_REPLAY_PROMPT.md` and `COMPARE_PROMPT.md` — reusable two-phase replay
  templates that keep production output hidden until the blind files are sealed.
- `retrieve_logs.py` and `analyze_reviews.py` — collection and indexing tools;
  they write restricted evidence and must be run only in a separate operator
  snapshot, never in this repository checkout.
- `PUBLICATION_MANIFEST.txt` — the complete allowlist for a public release.
- `SHA256SUMS` — integrity hashes for every other allowlisted file.
- `check_publication.py` — a heuristic pre-publication check for identifiers,
  source commits, registry IDs, and links outside this bundle.

Run the checks from this directory:

```sh
python3 check_publication.py
jq empty SUMMARY.json
sha256sum -c SHA256SUMS
```

Passing the check is necessary but not sufficient. A human must still review
the publication diff and confirm that qualitative descriptions cannot identify
a withdrawn or otherwise non-public submission.

Everything outside this directory is operator evidence and must be treated as
restricted unless separately reviewed and redacted.

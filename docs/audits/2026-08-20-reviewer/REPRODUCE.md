# Reproducing and continuing the reviewer audit

This runbook is safe to publish, but the audit it describes requires authorized
access to Palomar's reviewer artifacts and submission State. Commands that
produce restricted evidence must be run only in an operator-controlled
workspace.

## 1. Create an immutable dated snapshot

Use a new directory for every audit. Do not refresh an old snapshot in place.
The collection scripts derive their output root from their own location, so
copy the scripts into the new directory before running them.

```sh
audit_stamp=$(date -u +%Y-%m-%dT%H%M%SZ)
audit_dir="reviewer-audit-${audit_stamp}"
reviewer_checkout=$(git rev-parse --show-toplevel)
report_dir="$reviewer_checkout/docs/audits/2026-08-20-reviewer"
mkdir -p "$audit_dir"
cp "$report_dir/retrieve_logs.py" "$audit_dir/"
cp "$report_dir/analyze_reviews.py" "$audit_dir/"
mkdir -p "$audit_dir/public-report"
cp -R "$report_dir/." "$audit_dir/public-report/"
python3 --version > "$audit_dir/tool-versions.txt"
gh --version >> "$audit_dir/tool-versions.txt"
```

The copied public bundle is a template, not a current report. Replace its dates,
aggregates, replay results, and conclusions from the new snapshot before
considering publication, then regenerate its `SHA256SUMS`.

Record the audit cutoff, reviewer revision, policy revision, model identifier,
reasoning setting, and deployed workflow revision. Prefer machine-readable JSON
for this metadata. Do not put a full State git log in anything that may be
published: commit subjects can contain submission IDs and repository names.

## 2. Verify access without printing credentials

```sh
gh auth status
gh api repos/PalomarRegistry/PalomarSubmissionState --jq '.full_name'
```

Never use shell tracing while handling credentials. Do not place access tokens
in commands, logs, reports, or environment dumps. The GitHub CLI should obtain
credentials from its authenticated store.

## 3. Retrieve retained review packets

```sh
python3 "$audit_dir/retrieve_logs.py" \
  > "$audit_dir/retrieval.log" 2>&1
```

The collector lists all unexpired `review-packets-*` artifacts, downloads them
through the authenticated GitHub API, and retains prompts, raw events,
structured passes, final reviews, spend, workflow metadata, mechanical reports,
and the exact policy contract. It rejects absolute and parent-traversing archive
paths and records a SHA-256 digest for every retained member.

The collector is idempotent for artifacts already represented by an extraction
manifest. Because GitHub artifacts expire, a later run cannot necessarily
reconstruct an earlier snapshot. Preserve the dated evidence directory under
the appropriate restricted retention policy.

## 4. Pin the State snapshot

Clone or copy the submission State at the audit cutoff into
`$audit_dir/state-main`, then record only its HEAD hash in the restricted audit
metadata.

```sh
git clone --filter=blob:none \
  https://github.com/PalomarRegistry/PalomarSubmissionState.git \
  "$audit_dir/state-main"
git -C "$audit_dir/state-main" rev-parse HEAD \
  > "$audit_dir/state-main.commit"
```

The State snapshot is restricted. It can contain repository linkage,
submitter/account metadata, intake context, authorization evidence, token
digests, registration history, and deliberately removed identifiers recoverable
from history. Do not publish it or its git log.

## 5. Build the review index

```sh
python3 "$audit_dir/analyze_reviews.py" \
  > "$audit_dir/review-summary.json"
```

The analyzer deduplicates raw attempts and complete reviews by content hash,
distinguishes projected public reviews from complete internal reviews, joins
the review to the snapshot status, and summarizes decisions, scores, findings,
and spend.

Sanity checks:

```sh
jq empty "$audit_dir/artifact-inventory.json"
jq empty "$audit_dir/extraction-index.json"
jq empty "$audit_dir/review-index.json"
jq '.' "$audit_dir/review-summary.json"
```

Do not publish the indexes. They retain submission IDs, findings, source
metadata, workflow/artifact identifiers, and local evidence paths.

## 6. Inventory workflow failures separately

List the reviewer workflow with enough fields to distinguish queued, running,
completed, failed, and cancelled jobs:

```sh
gh run list \
  --repo PalomarRegistry/PalomarSubmissionState \
  --workflow reviewer.yml \
  --limit 1000 \
  --json databaseId,status,conclusion,createdAt,updatedAt,event,headSha,url \
  > "$audit_dir/reviewer-workflow-runs.json"
```

For a failed run, use `gh run view` or the Actions jobs API to determine the
actual job state and retrieve failed-step logs. Do not infer “model failure”
from a failed workflow conclusion. Classify at least:

- failure before model execution;
- model output rejected by deterministic validation;
- valid review followed by rendering, archival, registration, or API failure;
- mechanical verification failure; and
- cancellation or supersession.

Workflow logs are restricted and should never be copied into the public report.

## 7. Select a blind replay panel

Record the private case mapping in a restricted file that is not linked from the
public report. Apply explicit inclusion and exclusion rules before reading model
outcomes.

Recommended inclusion criteria:

- maintainer-authorized real submission;
- mechanical verification completed;
- reached editorial review;
- exact source commit is still recoverable; and
- sufficient source and metadata exist for independent inspection.

Exclude known probes, synthetic controls, internal technical tests, corrupted
packets, and cases whose exact commit cannot be recovered. Stratify the sample
across positive and adverse outcomes and, for a larger audit, across semantic,
definition, provenance, literature, notability, and operational failure modes.

Assign public aliases such as Case A, Case B, and so on. Do not publish the alias
map. Avoid publishing combinations of topic, date, disposition, or distinctive
complaint that could reidentify a withdrawn entry.

## 8. Run the contamination-controlled replay

Start a fresh session with `BLIND_REPLAY_PROMPT.md`. After the operator seals
its output, use `COMPARE_PROMPT.md` for the unblinded phase. Keeping these as
two controller actions is safer than asking one model session to enforce a
soft “do not read yet” boundary by itself.

Before unblinding, the replay model may inspect only:

- policy prompt templates, rubric, schemas, taxonomy, and materiality rules;
- a minimal case packet containing only fields needed for review;
- the mechanical report; and
- the source repository checked out at the exact submitted commit.

It must not inspect:

- rendered production prompts;
- production pass, raw, or final review output;
- existing audit conclusions or prior model comparisons; or
- filenames or summaries that reveal the production outcome.

The blind output should record:

- audit and case aliases;
- exact model identifier and reasoning setting;
- policy/reviewer/source revisions in the restricted record;
- outcome and all dimension scores;
- material findings, warnings, requested changes, and evidence;
- start/end times, usage, and cost when available; and
- the limitation that the replay is or is not routed through the deployed
  broker.

Seal the blind artifacts before opening production output:

```sh
sha256sum blind.json blind.md > blind.sha256
sha256sum -c blind.sha256
```

Never edit the blind files afterward. Write comparison and adjudication files
separately, then verify the blind hash again.

## 9. Adjudicate findings, not just outcomes

Production output is not ground truth. For every material finding from any
model, have an operator or independent expert record:

- the exact proposition being asserted;
- the supporting source lines and definitions;
- whether the proposition is true, false, or unresolved;
- whether it is material under the pinned policy; and
- which requested change, if any, is proportionate.

Calculate at least:

- adjudicated precision and recall of material findings;
- false-adverse and false-non-adverse rates;
- outcome agreement as a secondary metric;
- per-dimension score bias and dispersion;
- contract-valid reviews per raw model attempt;
- latency and estimated cost; and
- operational failure counts by phase.

With only five cases, report failure modes rather than population accuracy.
Target at least 25–50 stratified cases before making a model migration decision.

## 10. Produce a publication-safe report

Start from aggregate statistics and anonymized adjudication notes, not by
redacting a copy of the raw report. Redaction-by-deletion is easy to get wrong.

The public report must omit:

- submission, registry, workflow, artifact, and account identifiers;
- repository names and source commits;
- submitter identity, intake context, authorization evidence, and token hashes;
- State histories and exact per-case dates or dispositions;
- raw prompts, events, internal notes, logs, and evidence paths; and
- distinctive details that can reidentify a withdrawn submission.

Model complaints may be reported when they are generalized enough to explain
the evaluation result without recreating private linkage.

Run the bundle checker and inspect the diff manually:

```sh
find "$audit_dir/state-main/submissions" -mindepth 2 -maxdepth 2 \
  -name state.json -print0 \
  | xargs -0 jq -r \
    '[.id, .repository, .commit, .owner, .registered_entry, .push_proof.principal.login] | .[]? | select(type == "string" and length >= 6)' \
  | sort -u > "$audit_dir/private-publication-denylist.txt"
cd "$audit_dir/public-report"
python3 check_publication.py \
  --denylist "$audit_dir/private-publication-denylist.txt"
jq empty SUMMARY.json
# If the bundle is tracked in Git, inspect: git diff -- .
```

The denylist is restricted evidence and must not enter the public bundle.
Publish only files named in `PUBLICATION_MANIFEST.txt`. Passing the checker is
a heuristic, not a privacy guarantee.

## 11. Continue the audit

For each new snapshot:

1. create a new dated directory;
2. inventory and collect every artifact retained at the new cutoff, preserving
   the immutable old snapshot; optionally reuse prior bytes only after their
   artifact identity and archive digest match;
3. record model, policy, reviewer, workflow, and State revisions;
4. update aggregate operational metrics;
5. add a predeclared, stratified blind sample;
6. preserve sealed blind outputs and a restricted case map;
7. obtain human finding-level adjudication; and
8. publish a fresh anonymized report rather than editing history in place.

Build the longitudinal view by content hash across immutable dated snapshots;
do not mutate an older extraction manifest to make it look current.

A practical cadence is every 25 new complete reviews or monthly, whichever
comes first. Trigger an immediate audit when the model, policy schema, reviewer
orchestration, retrieval capability, or deterministic validation contract
changes.

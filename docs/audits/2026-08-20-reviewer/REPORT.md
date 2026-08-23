# Palomar AI reviewer audit

Publication-safe report for the snapshot ending 2026-08-20

Prepared 2026-08-21

## Executive summary

Palomar's production AI reviewer is doing valuable work and should remain the
primary editorial model, but its output should not be treated as infallible.
The review pipeline successfully fails closed when model output violates its
contract, and the production model can find subtle mathematical and
record-level defects. Its largest systemic weakness is literature assessment:
retrieval was frequently unavailable, yet literature scores remained tightly
clustered near the passing score.

A blind replay on five substantive live submissions compared the production
model with the Terra and Luna tiers. Terra agreed with the original production
outcome in two cases; Luna agreed in three, but returned all five submissions as
non-adverse. Both lower tiers missed a concrete contradiction in one adverse
record. The production model also made a confident technical mistake in a
different case, which a lower-tier replay correctly resisted.

The evidence does not support replacing the production model with Terra or
Luna. It does support adding a second, narrowly prompted verification step for
each proposed material finding. That verifier should attempt to falsify the
finding's exact technical premise; it should not vote on the whole review.

## Scope and evidence

The audit collected every retained reviewer packet available in the snapshot's
GitHub Actions artifact history. The retained evidence included model prompts,
raw event streams, structured pass outputs, final reviews, spend records,
mechanical reports, workflow metadata, and the exact policy contract. Bulky
source/database checkouts were omitted because their relevant revisions could
be recovered separately.

The snapshot contained:

- 141 retained review artifacts;
- 71 distinct raw model attempts;
- 67 complete, structurally valid reviews; and
- 233 reviewer workflow runs in the operational window examined.

The evidence archive is not part of this publication bundle. It contains
private operational and submission linkage and is retained only for authorized
operators.

## What the production reviewer does well

The production model demonstrated that it can inspect more than compilation
status. Across the full corpus it identified subtle issues involving selected
declaration strength, reachable-domain semantics, mathematical definition
fidelity, statement/source disagreement, and metadata scope. It also changed
its judgment when submitters changed the pinned source commit, which is the
behavior expected of a commit-specific reviewer rather than a repository
reputation system.

The pipeline's deterministic gates were effective. Four of 71 raw attempts
failed validation because of coverage, score-floor, or finding-preservation
violations, and those attempts did not silently become public reviews. Three
additional workflow failures occurred before model execution because deployed
schema versions disagreed. These failures were operational, not evidence that
the model had reviewed the mathematics and found it wanting.

This distinction matters: workflow failure, model-contract failure, mechanical
failure, and an adverse editorial decision should be counted and communicated
as different events.

## Systemic weaknesses

### Literature assessment is not independently verified

All 67 completed literature passes carried only qualified trust. In 40 cases,
the recorded external retrieval attempt failed at DNS resolution; in the other
27, no external retrieval attempt was recorded. Nevertheless, 56 of the 67
reviews assigned literature a score of 4 out of 5, and none assigned 5.

The score therefore behaved more like an assessment of the submission's own
literature narrative than an independent literature check. That can be useful,
but the interface should say so. Either provide an auditable retrieval service
or rename and visually qualify the dimension so a 4 is not mistaken for
external verification.

### Scores are compressed

Most dimensions cluster around 4 or 5. Compression makes the numeric profile
less informative than the findings and requested changes. It also makes
lower-tier score inflation easy to miss when outcome alone is compared.

### Operational failures dominate failed workflow counts

Of 233 reviewer workflow runs in the examined window, 172 succeeded, 34 failed,
and 27 were cancelled. Most failed runs involved registration, rendering,
archival, API, deployment-schema, or other orchestration problems rather than
failed AI judgment. Operational reporting should expose a separate “valid
review per model attempt” measure; it was 67 of 71, or 94.4%, in this snapshot.

## Blind Terra/Luna replay

### Selection

The replay used five substantive submissions from the live launch cohort. Each
was submitted by an authorized repository maintainer, passed mechanical
verification, and reached editorial review. Three ultimately registered; two
were later withdrawn. Known probes, synthetic controls, and submissions marked
as technical tests were excluded.

All cases are anonymized here. In particular, the report does not disclose
which cases were withdrawn or provide topic, repository, commit, submission, or
submitter information that would make that linkage recoverable.

### Blindness protocol

Each lower-tier model could inspect the production policy, rubric, schemas,
taxonomy, mechanical report, and source tree at the exact submitted commit. It
could not inspect the production model's rendered prompts, pass outputs, raw
events, final review, the audit conclusions, or an earlier model comparison.

Each blind JSON and Markdown result was saved and SHA-256 sealed before the
production review was opened. The production comparison was written separately,
and the blind hashes were reverified afterward.

These were model-tier re-reviews, not calls routed through the deployed review
broker. They isolate review judgment against real submissions but do not
reproduce every orchestration detail.

### Results

| Case | Production | Terra blind | Luna blind | Adjudication |
|---|---|---|---|---|
| A | adverse | non-adverse | non-adverse | Production had a legitimate record-classification concern but also a significant, incorrect semantic finding. The lower tiers were technically better on that premise but did not precisely identify the record defect. |
| B | non-adverse | adverse | non-adverse | Terra found a real provenance/auditability weakness but probably applied blocking materiality too strictly. A warning was better calibrated. |
| C | non-adverse | non-adverse | non-adverse | Substantive agreement; only one-point score calibration differed. |
| D | non-adverse | non-adverse | non-adverse | Substantive agreement. Luna's uniformly maximal editorial scores overstated certainty. |
| E | adverse | non-adverse | non-adverse | Production was clearly stronger. Both lower tiers missed an objective provenance contradiction and a selected-scope mismatch; Luna also over-scored the literature record. |

Raw outcome agreement was two of five for Terra and three of five for Luna.
Outcome agreement is not accuracy, because the production decision is evidence
rather than ground truth. Case A is the important counterexample: the
production model's central technical rationale was wrong even though another
record issue could still justify revision.

Luna's all-non-adverse pattern is more concerning than its raw 3/5 agreement
suggests. Terra was more willing to challenge provenance but was inconsistent
about which weaknesses were material. Neither lower tier is reliable enough to
be the sole reviewer on this sample.

## Recommendation

Keep the production model as the primary reviewer. Add an adversarial finding
verification stage before finalizing an adverse result:

1. Give the verifier one proposed material finding, the cited source lines,
   the relevant definitions, and the materiality rule.
2. Ask it to state the precise premise that must be true for the finding to
   hold, then attempt to disprove that premise.
3. Require an evidence-backed result of verified, contradicted, or unresolved.
4. Escalate contradicted or unresolved findings to the production model again
   or to a human. Do not let a lower-tier non-adverse verdict automatically
   erase a production finding.

Terra is the more promising shadow reviewer for provenance and materiality.
Luna may still be useful as a cheap technical skeptic, but it should not decide
whether the overall submission passes.

For evaluation, expand the blind sample to at least 25–50 cases and use human
adjudication at the finding level. Report precision and recall of material
findings, false-adverse and false-non-adverse rates, score bias, contract-valid
completion, latency, and cost. Whole-review outcome agreement should remain a
secondary measure.

## Limitations

- Five replay cases are enough to expose failure modes, not estimate population
  accuracy.
- The production review is not ground truth; case-level adjudication involved
  technical judgment and can itself be wrong.
- Model behavior is nondeterministic and model aliases may change. A continued
  audit must record exact model identifiers, policy and reviewer revisions,
  reasoning settings, date, and hashes of sealed outputs.
- The replay did not execute through the deployed broker, so it does not test
  broker-specific authentication, retries, environment, or rendering.
- Literature retrieval was too weak to support claims of independent coverage.

## Privacy and publication policy

Model criticism is not inherently sensitive. Linkage can be sensitive. A
withdrawn record may intentionally remove identifying details, and combining a
topic, exact commit, date, repository, or distinctive finding can recreate the
link that withdrawal removed.

This report therefore publishes aggregate results and anonymized case-level
reasoning only. It excludes:

- submission, registry, workflow, artifact, and account identifiers;
- repository names, source commits, submitter identities, and exact dates per
  case;
- full State histories, intake context, authorization proofs, token hashes, and
  registration metadata;
- raw prompts, event streams, model internal notes, workflow logs, and local
  paths into the evidence archive; and
- distinctive theorem titles or quotations that could identify a withdrawn
  submission.

The operator archive should be access-controlled and retained according to the
same policy as the underlying submission State. Only the files explicitly
listed in this bundle's publication manifest should be released.

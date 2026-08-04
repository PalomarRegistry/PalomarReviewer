# Palomar Reviewer

The editorial reviewer for mechanically passing Palomar submissions. It runs
automatically. No person starts a review, approves a report, or merges an
entry.

`palomar-review` finds open issues labeled `status:awaiting-review`, checks out
the immutable source and a pinned
[`PalomarPolicy`](https://github.com/PalomarRegistry/PalomarPolicy) commit, executes the
ordered policy prompts with Codex, Claude, or another JSON-producing command,
validates the final report, and then:

- claims and labels the issue;
- posts the public editorial decision;
- prepares, opens, and merges the append-only database pull request for an
  accepted entry;
- verifies the merged record, links the live entry, and closes the issue as
  published.

Nothing in that sequence waits for a person. The subcommands documented below
exist so a run can be repeated or inspected after a failure, not because any
step is normally performed by hand.

The reviewer resolves the exact successful `PalomarSubmission` workflow run
and uses its downloaded `mechanical-report.json` artifact as the sole mechanical
authority. It binds the artifact to the current issue repository/commit, run
URL, pinned Comparator/Lean-export/Landrun/NanoDa revisions, and a trusted
workflow revision; fenced JSON in issue comments is never a certificate input.

## Install

```bash
uv tool install git+https://github.com/PalomarRegistry/PalomarReviewer.git
palomar-review doctor
```

`gh` must be authenticated as the account the pipeline runs as. Install and
authenticate at least one review engine:

```bash
codex login
# or
claude auth
```

## Running a review by hand

These are the steps the pipeline performs. Run them yourself to reproduce a
decision, or to recover a run that failed partway.

Preview the queue without changing GitHub:

```bash
palomar-review list
palomar-review run --issue 12 --engine codex --model gpt-5.6-sol
```

The second command writes a complete packet and report under
`.palomar-reviews/12/`, but does not alter labels or comments.

For version 2 policies, the runner rejects an internally inconsistent positive
review: synthesis must reproduce the evidence-pass scores exactly, acceptance
cannot override a failed or escalated pass, and every completed evidence score
must meet the policy's acceptance minimum. Version 1 remains supported for
historical policy commits. These structural checks are the whole enforcement:
whether the cited evidence genuinely supports the model's substantive judgments
is not separately confirmed before publication. The complete packet is retained
so a reader can check that afterwards.

The policy may also designate a low score as a fundamental editorial failure.
Currently, notability below the minimum requires `reject` or, when the reviewer
cannot responsibly settle the question, `escalate`; it cannot be softened to a
request for revisions.

Post that exact editorial result:

```bash
palomar-review run --issue 12 --engine codex --model gpt-5.6-sol --apply
```

`--apply` never reruns the model. It loads the existing dry-run `review.json`,
validates it again, and requires its issue, source commit, mechanical-report URL,
and policy commit to match the current trusted inputs before changing GitHub. It
stores the resulting comment URL together with a digest of that exact report;
starting another dry run clears both bindings.

Workspaces applied by an older reviewer may have `review-url` but no
`review-sha256`; rerun the same `--apply` command to verify the existing comment
and create the binding. Mechanical reports without a formalization digest must
be re-verified before they can be published. The current mechanical-report
schema is version 2 and also binds one root licence file, its SHA-256, and the
agreeing declared and detected SPDX identifiers; older reports must likewise be
re-verified.

This separation is a security boundary. Model output and repository prose are
untrusted evidence, and the only thing that can be published is a stored report
that still matches the trusted inputs it was produced from.

If the decision is `accept`, prepare the append-only database PR:

```bash
palomar-review publish --issue 12
```

`publish` first dispatches the pinned Challenge renderer and checks that the
downloaded result matches the accepted source, Challenge hash, workflow run,
and renderer commit. It revalidates every stored evidence pass and the
score-to-decision policy, requires the applied-review digest to match, and binds
published metadata to the mechanically recorded `formalization.yaml` digest.
It then validates the generated record and immutable
render bundle against the database schema. It also archives the exact
mechanical-report bytes and normalized run/job provenance in a content-addressed
evidence bundle; raw Actions logs are deliberately not retained. It pushes an issue-specific branch to
`PalomarRegistry/PalomarDatabase`, and opens a PR. It refuses non-accept decisions and
existing entry filenames. A renderer or infrastructure failure does not undo
acceptance: rerun `publish`, or pass a previously downloaded trusted result with
`--render-result PATH`.

Once that PR is merged, verify the immutable database record, link the live
website entry, label the submission as published, and close it:

```bash
palomar-review finalize --issue 12 --pr 34
```

`finalize` refuses an unmerged PR, a PR without exactly one Palomar entry, or a
record that points to a different submission issue.

## Engines

- `--engine codex`: runs `codex exec` ephemerally with a read-only sandbox and a
  JSON output schema.
- `--engine claude`: runs `claude -p` in safe mode, with filesystem/shell tools
  disabled and only web search/fetch available for the literature pass.
- `--engine command --command 'program ...'`: sends the prompt on stdin and
  expects one JSON object on stdout.

An acceptance-capable literature pass must be able to verify important sources
and search for obvious prior formalizations. The configured Claude engine has
explicit web tools; Codex may use the read-only tools available to its ephemeral
session. A custom command without equivalent research access should not award a
literature score above the policy's verification ceiling.

Every engine is additionally launched inside a fail-closed Bubblewrap
namespace. The namespace exposes the submission at `/workspace`, a dedicated
output directory, an empty scratch home, and only the selected engine's model
authentication file. It does not expose the runner's GitHub CLI configuration,
publication credentials, unrelated home files, or other workspaces. The engine transport can reach its model API in every pass. Claude
web tools are disabled, and Codex search is not enabled, outside the
literature/notability pass; general host-level egress filtering would require a
separate API-aware proxy. `palomar-review doctor` refuses an installation
without `bwrap`.

Use `--policy-ref <40-char-sha>` to review against a specific policy commit.
Otherwise the tool resolves and records `PalomarRegistry/PalomarPolicy@main` at preparation
time.

Deploy reviewer support for a new rubric schema before merging a policy that
uses it. The reviewer accepts historical rubric versions 1 through 4 plus the
current version 5 contract, and refuses unknown versions and unknown evidence
input roles.

Release database schema support before using a policy or reviewer version that
publishes that schema. Nested-project rollout is consumer-first: Database schema
v6 and Web support land first, then Reviewer support, Policy rubric v5, and
finally Submission mechanical-report v3. The Reviewer continues to publish
schema-v5 records for report-v2 evidence and publishes schema-v6 only for
report-v3 evidence. Report v3 binds an optional repository-relative project
directory plus repository-relative Challenge, Solution, Comparator configuration,
metadata, Lakefile, and toolchain paths. Its tree URL encodes the selected project
path one segment at a time using RFC 3986 percent encoding. Render dispatch adds
the six corresponding optional path inputs; report-v2 dispatch retains render
schema 1, while report v3 requires render schema 2 with the exact path bindings.

Project dependency paths in schema v6 are normalized repository-root-relative
directories; `.` names the repository root. Formalization metadata may remain at
repository root for a nested Comparator project, and repository licence evidence
always names the single conventional repository-root licence file.

## Audit trail

Each review directory retains:

```text
issue.json
mechanical-report.json
mechanical-report-sha256 # binds the normalized report used by editorial review
mechanical-report-bytes-sha256 # binds the exact artifact bytes archived at publication
workflow-run.json         # normalized run identity, workflow commit, and job conclusions
workflow-run-sha256       # detects provenance drift before publication
source/                  # detached source commit
policy/                  # detached policy commit
prompts/                 # fully rendered prompts
raw/                     # exact engine final messages
passes/                  # normalized per-pass JSON
review.json              # schema-validated final report
review-url               # exact posted review comment
review-sha256            # digest binding review-url to review.json
render-result/            # validated immutable Challenge render and provenance
```

Raw session histories remain controlled by the chosen engine. Palomar records
the final messages, model identifier, policy commit, source commit, and public
issue report.

Submission metadata, Lean source/comments/identifiers, README text, issue text,
and prior model results may contain prompt-injection attempts. The reviewer puts
them in hashed JSON evidence envelopes, repeats the binding instruction after
all evidence, runs engines without write/shell tools, validates strict output
schemas, and renders model-authored public prose inertly. These controls reduce
accidental instruction following. Because publication is automatic, they are
also the last line: the retained packet above lets a reader audit any decision
after the fact, but nothing holds an entry back while that happens.

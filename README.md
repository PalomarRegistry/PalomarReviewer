# Palomar Reviewer

An operator-run tool for reviewing mechanically passing Palomar submissions.
Nothing here runs in CI.

`palomar-review` finds open issues labeled `status:awaiting-review`, checks out
the immutable source and a pinned
[`PalomarPolicy`](https://github.com/kim-em/PalomarPolicy) commit, executes the
ordered policy prompts with Codex, Claude, or another JSON-producing command,
validates the final report, and optionally:

- claims and labels the issue;
- posts the public editorial decision;
- prepares a database branch and pull request for an accepted entry.

The operator remains responsible for inspecting the report and merging the
database PR. The tool never merges.

The reviewer resolves the exact successful `PalomarSubmission` workflow run
and uses its downloaded `mechanical-report.json` artifact as the sole mechanical
authority. It binds the artifact to the current issue repository/commit, run
URL, and a trusted workflow revision; fenced JSON in issue comments is never a
certificate input. For indexed Challenge imports it independently checks out
each recorded repository/commit and verifies every source hash before adding
the actual imported files to definition-fidelity evidence.

## Install

```bash
uv tool install git+https://github.com/kim-em/PalomarReviewer.git
palomar-review doctor
```

`gh` must be authenticated as the operator. Install and authenticate at least one
review engine:

```bash
codex login
# or
claude auth
```

## Typical review

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
historical policy commits. The operator should still inspect whether the cited
evidence supports the model's substantive judgments.

The policy may also designate a low score as a fundamental editorial failure.
Currently, notability below the minimum requires `reject` or, when the reviewer
cannot responsibly settle the question, `escalate`; it cannot be softened to a
request for revisions.

After inspecting `review.json`, post that exact editorial result:

```bash
palomar-review run --issue 12 --engine codex --model gpt-5.6-sol --apply
```

`--apply` never reruns the model. It loads the existing dry-run `review.json`,
validates it again, and requires its issue, source commit, mechanical-report URL,
and policy commit to match the current trusted inputs before changing GitHub.
This separation is a security boundary: model output and repository prose are
untrusted evidence until an operator has inspected the stored report.

If the decision is `accept`, prepare the append-only database PR:

```bash
palomar-review publish --issue 12
```

`publish` first dispatches the pinned Challenge renderer and checks that the
downloaded result matches the accepted source, Challenge hash, workflow run,
and renderer commit. It then validates the generated record and immutable
render bundle against the database schema, pushes an issue-specific branch to
`kim-em/PalomarDatabase`, and opens a PR. It refuses non-accept decisions and
existing entry filenames. A renderer or infrastructure failure does not undo
acceptance: rerun `publish`, or pass a previously downloaded trusted result with
`--render-result PATH`.

After inspecting and merging that PR, verify the immutable database record,
link the live website entry, label the submission as published, and close it:

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
authentication file. It does not expose the operator's GitHub CLI
configuration, publication credentials, unrelated home files, or other
workspaces. Network access is absent for repository, statement, definition,
proof, and synthesis passes; it is enabled only for the literature/notability
pass. `palomar-review doctor` refuses an installation without `bwrap`.

Use `--policy-ref <40-char-sha>` to review against a specific policy commit.
Otherwise the tool resolves and records `kim-em/PalomarPolicy@main` at preparation
time.

Deploy reviewer support for a new rubric schema before merging a policy that
uses it. The reviewer accepts historical version 1 policies and the current
version 2 contract, and refuses unknown versions.

## Audit trail

Each review directory retains:

```text
issue.json
mechanical-report.json
source/                  # detached source commit
challenge-dependencies/  # detached indexed commits used by Challenge
challenge-review-sources.json # exact paths, versions, and hashes reviewed
policy/                  # detached policy commit
prompts/                 # fully rendered prompts
raw/                     # exact engine final messages
passes/                  # normalized per-pass JSON
review.json              # schema-validated final report
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
accidental instruction following; operator inspection of the dry-run report is
the final backstop.

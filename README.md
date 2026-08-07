# Palomar Reviewer

The editorial reviewer for mechanically passing Palomar submissions. No person
starts a review or approves a report.

`palomar-review` reads submissions from the private
[`PalomarSubmissionState`](https://github.com/PalomarRegistry/PalomarSubmissionState)
repository the submission server writes, checks out the immutable source and a
pinned [`PalomarPolicy`](https://github.com/PalomarRegistry/PalomarPolicy)
commit, executes the ordered policy prompts with Codex, Claude, or another
JSON-producing command, validates the final report, and then:

- delivers the review privately to the submitter, and to nobody else;
- once, and only once, the submitter chooses to register, prepares and opens the
  append-only database pull request for an accepted entry;
- verifies the merged record and records the registration against the private
  submission record.

The review is never posted in public. A decision the submitter does not register
leaves no public trace of itself, which is the whole reason the intake is
private. What is public from the moment of submission is the mechanical
verification: the repository, the commit, and the GitHub Actions run that
checked them, because that run is a public workflow with public logs.

The reviewer takes the mechanical report from the verification run the
submission server recorded for that submission, matched by run id and by exact
run name. The submission id appears in a public run title, so anyone able to
dispatch the workflow can produce a run carrying it; the name is therefore not
the trust boundary. The report is bound to the private record's repository,
commit, requested paths and authorization, and to pinned
Comparator/Lean-export/Landrun/NanoDa revisions on a workflow commit that is an
ancestor of `main`.

## Install

```bash
uv tool install git+https://github.com/PalomarRegistry/PalomarReviewer.git
palomar-review doctor
```

`gh` must be authenticated as an account with access to the private
`PalomarSubmissionState` and private `PalomarDatabase` repositories. `doctor`
checks both API visibility and an authenticated Git read of the database.
Registration passes the short-lived `gh auth token` to Git through an
environment-only HTTP header, never a command-line argument; the same identity
must be able to push registration branches and open pull requests. Anything
that changes a submission's record refuses
to run unless `PALOMAR_ALLOW_STATE_WRITES=1` is set, because that record is
live and private and writing to it should be deliberate. Registration also
requires `PALOMAR_ARCHIVE_TOKEN`, belonging to the dedicated machine account
that can create and write forks in the `PalomarArchive` organization. Install
and authenticate at least one review engine:

```bash
codex login
# or
claude auth
```

## Running a review by hand

These are the steps the pipeline performs. Run them yourself to reproduce a
decision, or to recover a run that failed partway. Submissions are named by the
twelve-character id the submission server allocated.

Preview the queue without changing anything:

```bash
palomar-review list
palomar-review run --submission a1b2c3d4e5f6 --engine codex --model gpt-5.6-sol
```

The second command writes a complete packet and report under
`.palomar-reviews/a1b2c3d4e5f6/` and changes nothing else.

The queue itself is `index/open.json` in the private state repository: the
submissions the reviewer is not yet finished with. The submission server adds an
id when it admits one, and a pass drops one when the record says there is
nothing left to do to it, so a pass costs the queue rather than the size of the
registry. It is derived rather than authoritative: an index that is missing,
damaged, from another contract, or more than a week old is rebuilt from a
checkout of every record, and deleting the file makes the next pass rebuild it
at once. `palomar-review rebuild-queue` derives it on demand, which is what the
weekly sweep runs: a rebuild is the one thing here that costs the size of the
whole registry, so it belongs on a schedule rather than falling out of whichever
pass crosses the window. A pass that cannot enumerate its work fails; it never reports having
found nothing.

For rubric version 2 and later, the runner rejects an internally inconsistent
positive review: synthesis must reproduce the evidence-pass scores exactly, acceptance
cannot override a failed pass, and every completed evidence score
must meet the policy's acceptance minimum. These structural checks are the whole
enforcement: whether the cited evidence genuinely supports the model's
substantive judgments is not separately confirmed. The complete packet is
retained so a reader can check that afterwards.

Rubric version 7 additionally rejects a substantive pass unless its
`declarations_checked` manifest exactly matches every theorem and definition in
the mechanically verified Comparator configuration, in configuration order.
This prevents a multi-declaration submission from being reduced to a
reviewer-selected headline. Clean declarations need no public comment; the
policy requires every distinct material criticism to survive synthesis, and
the runner checks that the final AI-comment list reproduces every warning and
error from the evidence passes in order.

One review consumes the single Comparator configuration path explicitly chosen
at intake. Different configuration paths at the same repository and commit are
different Palomar entries. Registration also binds an existing Palomar ID to
its configuration path, so an update cannot silently switch result sets.

The policy may also designate a low score as a fundamental editorial failure.
Currently, notability below the minimum requires `reject`; it cannot be
softened to a request for revisions. Other failed passes may lead to `revise`
when the synthesis identifies a specific, realistically correctable gap.

Deliver that exact review to the submitter:

```bash
palomar-review run --submission a1b2c3d4e5f6 --engine codex --model gpt-5.6-sol --apply
```

`--apply` never reruns the model. It loads the existing dry-run `review.json`,
validates it again, and requires its submission, source commit,
mechanical-report URL and policy commit to match the current trusted inputs. It
writes the review into the private state repository, records the digest of what
it delivered, and clears any consent given to an earlier review: consent is to a
particular review, not to registering at large.

This separation is a security boundary. Model output and repository prose are
untrusted evidence, and the only thing that can be delivered is a stored report
that still matches the trusted inputs it was produced from.

## Registration

Nothing is registered until the submitter asks for it on their status page. When
they have, and the review was an acceptance:

```bash
palomar-review register --submission a1b2c3d4e5f6
```

`register` authorises first, before anything public happens. It requires the
private record to hold a delivered review, to show proved write access, to name
no previous registration, to carry the submitter's consent, and for the digest
delivered, the digest consented to, and the review about to be archived to be
the same bytes. Only then does it dispatch the pinned Challenge renderer, which
is a public Actions run naming the repository and commit and would otherwise
signal an acceptance the submitter never agreed to register.

It then checks that the render matches the accepted source, Challenge hash,
workflow run and renderer commit, revalidates every stored evidence pass and the
score-to-decision policy, binds registered metadata to the mechanically recorded
`formalization.yaml` digest, and preserves the submitted repository, every Git
dependency, and any separately recorded substantive formalization. Repositories
in the same GitHub fork network share one native fork in `PalomarArchive`; each
accepted commit receives a record-specific
`refs/tags/palomar/PALOMAR-…-vN/<sha>` ref. If any source, fork, commit, or ref
cannot be created and read back exactly, registration stops before a database
branch is published. GitHub creates forks asynchronously, so the reviewer waits
for their Git objects and ref-writing endpoint to become ready before treating
a preservation operation as failed. Preservation refs are written with an
authenticated Git push, which also transfers a commit that GitHub has not yet
copied into the new fork. If a source repository has been renamed or
transferred since verification, the reviewer uses GitHub's returned canonical
name for archive operations while retaining the submitted location in the
preservation receipt.

The generated record and render bundle are then validated against the database
schema. It archives the exact mechanical-report
bytes, the normalized run and job provenance, and the review itself in one
content-addressed evidence bundle together with `source-archive.json`, so a
single tree hash covers everything justifying the record; raw Actions logs are
deliberately not retained. It pushes a branch to
`PalomarRegistry/PalomarDatabase` and opens a PR.

The automatic finalizer reads GitHub's aggregate mergeability state and merges
only when it is `CLEAN`; pending or failed database checks remain `UNSTABLE`.
This avoids granting the reviewer separate access to individual check-run
details while preserving the all-green publication gate.

`PalomarArchive` and its `PalomarArchivist` machine account are operator-created
GitHub resources; the workflow does not sign up an account. Add the account as
an ordinary organization member and store its credential as
`PALOMAR_ARCHIVE_TOKEN`. Registration verifies the credential's identity,
creates a repository-level ruleset on each new public fork that permits new
`refs/tags/palomar/**/*` tags but prevents updating or deleting existing ones,
and then removes the machine account's repository-admin grant so it falls back
to the organization's base Write role. The account makes native forks, adds
new preservation refs, and stars the original top-level source after its
registration completes. `palomar-review star-registered` is idempotent: it
verifies each star before recording it in private state, and an interrupted or
failed pass is retried without affecting the accepted record. Dependencies and
archive forks are not starred. GitHub redacts a
ruleset's bypass-actor list after that demotion; the reviewer verifies every
remaining ruleset field on later registrations, while creation verifies the
complete rule, including its empty bypass list, before dropping administrator
access.

A renderer or infrastructure failure does not undo acceptance. Before making
any public archive changes, `register` reserves the permanent ID and version in
the private submission state. A retry reuses that identity and verifies or
finishes the same archive refs instead of allocating an orphaned second ID.
Rerun `register`, or pass a previously downloaded trusted result with
`--render-result PATH`.

Permanent identifiers are `PALOMAR-YYYY-MM-DD-NNNNNN`, where the serial follows
the largest one that date has already used, starting at 1 for a date with none.
The serial was drawn at random until 2026-08-07, to hide how many reservations
never became records. What that cost was the ordering: with a random serial, the
order two identifiers were registered in cannot be read from the identifiers, so
anything wanting registration order has to carry an ordinal beside the
identifier, and an ordinal that disagrees with its identifier is a failure
nothing downstream can detect or repair. Serials rise within a date and dates
never go backwards, because a new version of an existing result reuses its first
version's identifier rather than allocating one, so ordering identifiers as
strings is registration order across the whole registry.

Once the PR is merged, verify the immutable record and close out the private
submission:

```bash
palomar-review finalize --submission a1b2c3d4e5f6 --pr 34
```

`finalize` refuses an unmerged PR, a PR without exactly one Palomar entry, or a
record that names a different submission.

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

Every engine is additionally launched inside a fail-closed Bubblewrap namespace.
The namespace exposes the submission at `/workspace`, a dedicated output
directory, an empty scratch home, and only the selected engine's model
authentication file. It does not expose the runner's GitHub CLI configuration,
registration credentials, unrelated home files, or other workspaces. The engine
transport can reach its model API in every pass. Claude web tools are disabled,
and Codex search is not enabled, outside the literature/notability pass;
general host-level egress filtering would require a separate API-aware proxy.
`palomar-review doctor` refuses an installation without `bwrap`.

The reviewer is told the repository, commit, authorization, update intent and
the submitter's notes. It is deliberately not told who submitted: a review
assesses the work, and a model that knows who sent it can be swayed by that.

Use `--policy-ref <40-char-sha>` to review against a specific policy commit.
Otherwise the tool resolves and records `PalomarRegistry/PalomarPolicy@main` at
preparation time.

Treat a rubric or final-review schema bump as a coupled Policy and Reviewer
deployment. Pause new review starts, confirm that no review is in flight, merge
Policy and then Reviewer back-to-back, and resume the runner. A current reviewer
refuses a policy checkout or stored review from another contract version rather
than delivering or registering it. The automatic loop queues an already
delivered review from an older contract for a fresh review before it can be
registered.

## Audit trail

Each review directory retains:

```text
state.json                     # the private submission record under review
mechanical-report.json
mechanical-report-sha256       # binds the normalized report used by editorial review
mechanical-report-bytes-sha256 # binds the exact artifact bytes archived at registration
workflow-run.json              # normalized run identity, workflow commit, job conclusions
workflow-run-sha256            # detects provenance drift before registration
source/                        # detached source commit
policy/                        # detached policy commit
prompts/                       # fully rendered prompts
raw/                           # exact engine final messages
passes/                        # normalized per-pass JSON
review.json                    # schema-validated final report
review-sha256                  # digest of the review delivered to the submitter
render-result/                 # validated immutable Challenge render and provenance
```

Raw session histories remain controlled by the chosen engine. Palomar records
the final messages, model identifier, policy commit, source commit, and the
review itself. Reviews are private, not confidential: they are readable by
Palomar operators, by GitHub, and by the model provider, and are retained
indefinitely so that any decision can be audited.

Submission metadata, Lean source, comments and identifiers, README text, the
submitter's notes, and prior model results may contain prompt-injection
attempts. The reviewer puts them in hashed JSON evidence envelopes, repeats the
binding instruction after all evidence, runs engines without write or shell
tools, and validates strict output schemas. These controls reduce accidental
instruction following. The retained packet above lets a reader audit any
decision after the fact.

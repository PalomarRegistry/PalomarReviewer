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
checks API visibility and an authenticated Git read of the database, and the
archive token's identity. It does not check `PalomarSubmissionState`, so a
credential that can read the database and not the state repository passes it and
fails on the first pass instead.
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

## Running the tests

```bash
python -m unittest discover -s tests
```

Some of what the suite checks is not in this repository, and it will tell you
at the end of the run exactly what it therefore did not check:

| capability | provide | what it buys |
| --- | --- | --- |
| `schema` | `PALOMAR_SCHEMA_CHECKOUT`, or `PALOMAR_DATABASE_CHECKOUT` | validating a built record against the schema the database serves |
| `database` | `PALOMAR_DATABASE_CHECKOUT` | registering into a real PalomarDatabase checkout end to end |
| `policy` | `PALOMAR_POLICY_CHECKOUT` | checking a review against the live PalomarPolicy rubric |
| `sandbox` | `bwrap` on `PATH` | running an engine inside a real Bubblewrap namespace |

Interactively an absent capability skips or narrows the tests that need it, and
is named in a summary the run prints when it finishes. Under CI it fails the
run instead, unless the workflow named it in `PALOMAR_TESTS_WITHOUT`. That
exists because the alternative had already cost us: two tests validated a
record against the served schema only if `PALOMAR_SCHEMA_CHECKOUT` happened to
be set and passed silently when it was not, and the one end-to-end
registration test needed `PALOMAR_DATABASE_CHECKOUT`, which no workflow set, so
it skipped every run and was red for days before anybody looked.

This repository's own CI declares `PALOMAR_TESTS_WITHOUT: database`, because
PalomarDatabase is private and public CI is deliberately given no credential
for it. The end-to-end registration test runs instead in PalomarDatabase, in
`.github/workflows/reviewer-contract.yml`, daily and on any pull request there
that touches a schema, the validator or the record fixtures.

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

The reviewer accepts only the current rubric contract (schema version 7). It
rejects an internally inconsistent positive review: synthesis must reproduce
the evidence-pass scores exactly, acceptance
cannot override a failed pass, and every completed evidence score
must meet the policy's acceptance minimum. These structural checks are the whole
enforcement: whether the cited evidence genuinely supports the model's
substantive judgments is not separately confirmed. The private State workflow
uploads the complete packet as operator evidence with a requested 90-day
artifact lifetime; that operational setting is not an author-access promise.

The current contract also rejects a substantive pass unless its
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

## Review usage accounting

Each rubric pass records the engine's `turn.completed` usage evidence under
`spend.json` and, when the review is delivered, in the private submission
record. A Codex completed turn is an aggregate across the model requests made
while the agent handles that pass; it is not request-level usage. The record
therefore keeps each raw turn-usage object, including `total_tokens` when
emitted, plus an explicit status and reason when usage is absent, malformed, or
ambiguous. Usage accounting never discards an otherwise successful review.

Production uses `codex:gpt-5.6-sol`. At the current list prices, ordinary input
is $5.00/M tokens, cached input is $0.50/M, cache-write input is 1.25 times the
ordinary input rate, and output is $30.00/M. A request with more than 272,000
input tokens is charged at 2 times input and 1.5 times output, and that threshold
applies to each model request—not to the completed-turn aggregate. See the
[official GPT-5.6 Sol model page](https://developers.openai.com/api/docs/models/gpt-5.6-sol).

When a valid turn aggregate has at most 272,000 total input tokens, every
constituent request is necessarily below the long-context threshold. The
ordinary, cached, cache-write, and output categories are then linear, so the
runner can display an exact current base-rate total. When a turn aggregate is
larger, it does not apply the long-context multiplier to the aggregate: some,
all, or none of its constituent requests may have crossed the threshold. It
reports that exact USD is unavailable until Codex exposes request-level usage.

For a valid aggregate, `input_tokens` is total input and cached and cache-write
input are subsets, so both are subtracted once to derive ordinary input.
Reasoning tokens are already included in output and are not charged again.
OpenAI's
[current model guidance](https://developers.openai.com/api/docs/guides/latest-model)
says to track both `cached_tokens` and `cache_write_tokens`, although the public
Responses usage reference still documents only the cached-token detail. The
reviewer preserves the Codex usage object rather than inventing a stronger
cache-read/cache-write distinction.

Existing pre-launch usage records are schema v1 and carry only a null `usd`
placeholder. The reviewer can still read them when they are present in the
cumulative private history. New records use schema v2 and contain the raw
per-turn aggregates and evidence status, with no top-level aggregate or
vendor-dollar field. Current dollar figures are only an operator-facing run
summary when the evidence is sufficient.

## Registration

Nothing is registered until the submitter asks for it on their status page. When
they have, and the review was an acceptance:

```bash
palomar-review register --submission a1b2c3d4e5f6
```

`register` authorises first, before anything public happens. It requires the
private record to hold a delivered review, to say how push access was proved and
not merely that it was, to name
no previous registration, to carry the submitter's consent, and for the digest
delivered, the digest consented to, and the review about to be archived to be
the same bytes. Only then does it dispatch the pinned Challenge renderer, which
is a public Actions run naming the repository and commit and would otherwise
signal an acceptance the submitter never agreed to register.

An acceptance normally remains an offer to register for 24 hours after the
review is delivered. A review-contract or security change may require immediate
reverification instead; this exception avoids retaining obsolete validators as
compatibility code. After 24 hours Palomar may expire the offer and require
reverification before making a new one. The offer may remain usable longer,
but there is no promise that it will.

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

The generated record is then validated against `schema-v2.json`, and the scores
that decided it against `scores-v1.json`. The scores go beside the record, in
`scores/<id>-vN.json`, and not inside it. There is one record schema again
because of that: while the publisher stripped the scores on the way out, a
published record's bytes were a function of publisher code rather than of the
commit, so the contract the record was built against and the schema of the data
served were two documents, and a record could satisfy one and fail the other.
The render bundle is checked by the reviewer's own render validation and then by
the database's `tools/validate.py`. It archives the exact mechanical-report
bytes, the normalized run and job provenance, and the review itself in one
content-addressed evidence bundle together with `source-archive.json`, so a
single tree hash covers everything justifying the record; raw Actions logs are
deliberately not retained. It pushes a branch to
`PalomarRegistry/PalomarDatabase` and opens a PR.

The automatic finalizer merges only when GitHub reports the change `CLEAN` and
the database's own `validate.yml` run for that exact head commit completed
successfully. `CLEAN` alone is not enough and was believed for a while: the
database has no enforced branch protection, so there are no required checks for
GitHub to withhold `CLEAN` over, and it says `CLEAN` in the seconds after the
change is opened, before Actions has started anything. Merging on it alone
registers a record whose validation had not run. The outcome is read from the
Actions API rather than from the pull request's check rollup, because reading
that rollup needs a permission fine-grained tokens do not offer under any name;
the reviewer credential therefore needs `Actions: read` on `PalomarDatabase`. A
validation the credential cannot see is never a reason to merge, and the merge
itself is pinned with `--match-head-commit`.

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

Permanent identifiers are `PALOMAR-YYYY-MM-DD-NNNNNN`. The date is the day the
result entered the registry, which is the day the submitter's consent was acted
on, and the serial follows the largest one that date has already used, starting
at 1 for a date with none.

The date is deliberately not the date of the review. A Palomar date is a
priority claim, and nothing is registered until the submitter consents. The
normal offer window is 24 hours, subject to immediate reverification after a
review-contract or security change, and an offer may remain usable longer
without a promise. Whenever registration happens, a date taken from the review
would let waiting buy an earlier position ahead of results registered meanwhile.
Waiting therefore costs a later position instead. The record carries the
review's own timestamp separately, as `review.reviewed_at`, which nothing orders
by.

The record carries the moment of registration too, as `registered_at`, and the
date in the identifier is the day of it. Both come from one reading of the
clock, so a registration that starts a second before midnight cannot end up
with an instant on one day and an identifier on another. Every ordering surface
in the database reads `registered_at`: the landing page, the feeds and the
subject pages. It is per version, because a v2 is a new registration and is
news, where the result's date would file it among the results registered in the
year of its v1. `accepted_at` stays the result's date and is inherited by every
later version, because it is what the identifier carries and what the browse
page is read from, and the database refuses a version 1 whose two dates
disagree.

The serial was drawn at random until 2026-08-07, to hide how many reservations
never became records. What that cost was the ordering: with a random serial, the
order two identifiers were registered in cannot be read from the identifiers, so
anything wanting registration order has to carry an ordinal beside the
identifier, and an ordinal that disagrees with its identifier is a failure
nothing downstream can detect or repair. Serials rise within a date, and a new
version of an existing result reuses its first version's identifier and with it
that version's date rather than allocating either, so ordering identifiers as
strings is registration order across the whole registry. The one thing that can
put an older identifier in late is the reservation above: a run that failed
before midnight and is retried after it finishes under the date it reserved,
which is one identifier committed late and no other identifier moved.

Once the PR is merged, verify the immutable record and close out the private
submission:

```bash
palomar-review finalize --submission a1b2c3d4e5f6 --pr 34
```

`finalize` refuses an unmerged PR, a PR without exactly one Palomar entry, or a
record that names a different submission.

## Engines

- `--engine codex`: runs `codex exec` ephemerally with its normal inspection
  and shell tools, a read-only Codex sandbox, and a JSON output schema.
- `--engine claude`: runs `claude --print` in safe mode. Its explicit tool
  allowlist is empty for ordinary passes and contains only `WebSearch` and
  `WebFetch` for the literature pass; it has no filesystem or shell tool there.
- `--engine command --command 'program ...'`: runs the named program, sends the
  prompt on stdin, and expects one JSON object on stdout. The program is not a
  tool-restricted model engine; its abilities are those of arbitrary code
  inside the outer namespace.

An acceptance-capable literature pass must be able to verify important sources
and search for obvious prior formalizations. Only the configured Claude
literature pass currently has explicit web research tools. This runner never
enables Codex web search and ignores user configuration that could enable it.
Codex, and any custom command without equivalent research access, therefore
must not award a literature score above the policy's verification ceiling.

Every engine is additionally launched inside a fail-closed Bubblewrap namespace.
The namespace exposes the submission at `/workspace`, a dedicated output
directory, an empty scratch home, and only the selected engine's model
authentication file. It does not expose the runner's GitHub CLI configuration,
registration credentials, unrelated home files, or other workspaces. The engine
transport can reach its model API in every pass. Claude web tools are disabled,
outside the literature/notability pass; this runner never enables Codex web
search and ignores Codex user configuration. General host-level egress
filtering would require a separate API-aware proxy.
`palomar-review doctor` refuses an installation without `bwrap`.

This containment is not a credential broker. For Codex and Claude, the selected
engine's own authentication file is deliberately bound into its namespace;
Codex can read files and run shell commands there. The namespace keeps other
operator credentials out and prevents repository writes, but its shared
network and engine-process-visible authentication material leave a residual
prompt-injection and exfiltration risk. The output credential check catches a
key copied out in plain text; it is a backstop, not proof against encoding or
another channel.
The planned broker boundary will keep provider credentials outside the engine
namespace and expose only a narrow authenticated model transport. That broker
does not exist yet.

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

During a review, its private work directory contains:

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
spend.json                     # raw turn-aggregate usage evidence; no historical USD value
review.json                    # schema-validated final report
review-sha256                  # digest of the review delivered to the submitter
render-result/                 # validated immutable Challenge render and provenance
```

Raw session histories remain controlled by the chosen engine. Palomar records
the final messages, turn-aggregate usage evidence, model identifier, policy
commit, source commit, and the review itself. The private State workflow uploads
the complete packet as an Actions artifact with a requested 90-day operational
lifetime. That packet is operator evidence, not an author-access service or the
24-hour registration-offer promise. Reviews are private, not confidential: they
are readable by Palomar operators, by GitHub, and by the model provider.

Submission metadata, Lean source, comments and identifiers, README text, the
submitter's notes, and prior model results may contain prompt-injection
attempts. The reviewer puts them in hashed JSON evidence envelopes, repeats the
binding instruction after all evidence, exposes the submission read-only,
restricts Claude's tool list, runs Codex with a read-only sandbox, isolates host
files with Bubblewrap, and validates strict output schemas. Codex still has
shell tools, a custom command is arbitrary code inside the namespace, and the
selected engine credential remains exposed as described above. These controls
reduce accidental instruction following; they do not create the planned
credential-broker boundary. While the private packet remains available, it lets
an operator audit the decision.

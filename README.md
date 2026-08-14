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

`palomar_reviewer.mechanical` is the current mechanical-evidence contract. It
owns the report schema, safe repository-path resolution, and the data bindings
among the report, private submission record, and recorded workflow run. The CLI
remains the composition root: it selects and downloads the server-recorded
artifact, applies that contract before cloning source, and only then asks
GitHub whether the validated workflow commit is still on `main`'s lineage.
The path helpers inspect filesystem metadata only inside a caller-supplied
source checkout; the module reads no file contents and has no network,
subprocess, environment, clock, or state-repository access.
Registration separately rechecks the stored report bytes, semantic digest, and
workflow-run digest before using an inspected workspace.

Failed preflight and full runs use the same trust boundary. `ingest-failures`
downloads the exact recorded run's mode-specific artifact, validates its
submission/source binding, redacts it to the diagnostics-v1 contract, and writes
the failure together with its terminal status. A missing or untrusted artifact
becomes an explicit Palomar-owned diagnostic rather than a submitter fault.

`repair-queue` handles the separate metadata-repair outbox. It accepts only the
exact versioned profile allowlist, including bounded structured people, source,
automation, and substantive-repository values. It creates missing mappings and
round-trips valid YAML without replacing the whole file, refuses aliases and
malformed YAML, and pushes only to an
Actions-disabled fork in `PalomarRepairs` using `PALOMAR_REPAIR_TOKEN`. Before
opening a PR it runs the candidate commit through
`PalomarSubmission/scripts/verify_submission.py prepare` from `main`, the same
entry point as ordinary preflight. That subprocess runs in the same fail-closed
Bubblewrap namespace the review engines run in, and no host without bubblewrap
runs it at all. What it is given is a `git archive` export of the Submission
checkout at `HEAD` rather than the working tree, so neither `.git/config`, whose
extraheader can carry the credential the checkout was made with, nor any
untracked file beside it is reachable; the runner's interpreter, Git and
Bundler read-only, never through a mount that would carry the operator's home,
workspace, or temporary directory in with it; one writable directory; `/dev/null`
on standard input; and an environment built from an exact list of names.
That list is exact rather than prefixed because Bundler documents credentials in
`BUNDLE_`-prefixed variables (`BUNDLE_GITHUB__COM` and every other
`BUNDLE_<HOST>__<TLD>`), and a proxy variable whose URL carries userinfo is a
refusal rather than something to pass on. Bubblewrap itself is started with that
same environment, because it stays at PID 1 inside the namespace and
`/proc/1/environ` is readable from in there. Neither State write authority nor
either GitHub token crosses into candidate-controlled intake.

The documented residual is the network. `prepare` resolves the candidate commit
by fetching it, Bubblewrap shares the host's network view or nothing at all, and
it cannot filter egress; so repair preflight is supported only on an ephemeral
runner with no private routing and no managed identity or instance-metadata
endpoint that grants anything, which is what a GitHub-hosted runner is. A
self-hosted runner that can reach an internal network is not a supported place
to run it. Failed candidate validation leaves an
actionable explanation and manual patch, removes the temporary repair branch,
and never opens a knowingly failing PR. Finished pull requests also have their
exact deterministic repair branch removed on a best-effort basis.

`upgrade-repair-failures` reruns current preflight for settled profile-1
`formalization.missing_sections` failures and replaces only their failure
guidance with profile-2 diagnostics and safe draft values. The submission id,
status, original run link, and history are retained, and a migration event is
appended. Pass `--submission ID` for one record or omit it for all eligible
records; as with every State mutation, the operator must explicitly set
`PALOMAR_ALLOW_STATE_WRITES=1`.

`palomar_reviewer.authorization` is the pure registration-authorization
contract. It owns the accepted push-proof methods, binds the private State
record to the report and review, requires the positive one-time consent state,
and checks that the delivered, consented-to, and registering review have one
canonical digest. New work applies the complete contract after the
credential-output backstop, accepted-decision check, and stored
report/run/policy bindings and before source preservation, render, or database
work. Recovery of an already-created branch applies the narrower current
State/review/source standing first, then validates the reserved public record
instead of repeating those side effects. The module performs no retrieval,
filesystem access, subprocess, network, State write, or public action.

Mechanical review accepts the explicit `technical-test` relationship so an
operator can exercise the pipeline honestly. Registration authorization rejects
that relationship, its State marker, or its distinct proof method before any
public side effect, even if a consent flag was forged.

`palomar_reviewer.checkpoint` owns the saved-attempt, deterministic-branch,
same-repository PR, immutable-record, and idempotent State-checkpoint contract.
The CLI supplies its GitHub read/create and conditional State-write adapters;
the module has no dependency back into the CLI and exposes no compatibility
path for a branch or record outside the one current reserved shape.

## Install

For development or a source checkout:

```bash
uv tool install git+https://github.com/PalomarRegistry/PalomarReviewer.git
palomar-review doctor
```

Production does not use that source-install command. `runtime/` holds the
promoted Reviewer wheel, its complete hash-locked Python requirements, the
reviewed Codex npm package and platform-binary lock, and a `SHA256SUMS`
manifest. State workflows independently pin both the Git commit containing the
files and the digest of `SHA256SUMS`, then use Python 3.11.10. They install the
third-party Python wheels under
`--require-hashes --no-deps --only-binary :all:` before installing the verified
local Reviewer wheel with `--no-deps --no-index`. There is no package-version
or source-build choice at runtime: the only acceptable local Reviewer wheel and
downloaded dependency wheels are the bytes reviewed in the manifest and
`uv.lock`. `npm ci` similarly verifies the exact SRI digest of both the Codex
wrapper and the Linux x64 binary selected by the reviewed lock; lifecycle
scripts are disabled and the installed CLI must identify itself as 0.147.0.

Regenerate a proposed promotion with exact uv 0.12.1 on x86-64 Linux:

```bash
python3 tools/runtime_artifact.py --write
python3 tools/runtime_artifact.py --check
```

The build uses Python 3.11.10, a hash-locked Hatchling build graph, and a fixed
ZIP epoch; CI reproduces the wheel byte-for-byte and performs a cold install.
The current `runtime/` metadata and Reviewer wheel are about 137 KiB; npm still
downloads the 116 MiB compressed Codex Linux artifact on each run. Any commit that changes the
packaged Reviewer source, `README.md`, `LICENSE`, or project packaging metadata
must regenerate its roughly 118 KiB wheel and tiny manifest; a dependency change
also replaces the roughly 15 KiB requirements file. Python installation still
depends on PyPI and Codex installation on the npm registry, but neither index
can substitute different bytes without breaking the reviewed digest. The
pinned checkout, GitHub runner image, Node/npm, and exact setup actions/uv
binary remain bootstrap trust; Ubuntu packages remain outside this artifact.

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
that can create and write forks in the `PalomarArchive` organization.

Install the reviewed Codex lock, and give the reviewer the provider key under
the upstream-only name the broker reads:

```bash
npm ci --prefix codex-runtime --ignore-scripts --no-audit --no-fund
export PATH="$PWD/codex-runtime/node_modules/.bin:$PATH"
export PALOMAR_OPENAI_UPSTREAM_KEY=...   # a dedicated Palomar API key
palomar-review doctor
```

That key belongs to a dedicated Palomar OpenAI project, not to a person's
interactive Codex login, and who owns its billing and who can revoke it are
recorded privately. It is read only by the reviewer process, which hands it to
the loopback broker described under [Engines](#engines) and never to Codex.
`OPENAI_API_KEY` is deliberately not consulted: a variable Codex would pick up
by itself is exactly the ambient authentication the broker exists to remove, so
a reviewer configured only with that name refuses to run rather than
authenticating around its own boundary. Provider-side spend limits and alerts
are worth configuring as a second layer; they do not replace the per-pass
ceilings the broker enforces.

`claude auth` still authenticates the Claude engine, which is not a production
engine: see [Engines](#engines).

## Running the tests

```bash
python -m unittest discover -s tests
uv run --isolated --python 3.11.10 --locked --only-group dev ruff check .
```

The lint command uses the exact Ruff release and artifact hashes recorded in
`uv.lock` in a temporary environment, so it does not replace packages in a
project `.venv`; CI runs the same locked command. `--locked` also refuses any
dependency edit whose lockfile has not been regenerated with `uv lock`.

Some of what the suite checks is not in this repository, and it will tell you
at the end of the run exactly what it therefore did not check:

| capability | provide | what it buys |
| --- | --- | --- |
| `schema` | `PALOMAR_SCHEMA_CHECKOUT`, or `PALOMAR_DATABASE_CHECKOUT` | validating a built record against the schema the database serves |
| `database` | `PALOMAR_DATABASE_CHECKOUT` | registering into a real PalomarDatabase checkout end to end |
| `policy` | `PALOMAR_POLICY_CHECKOUT` | checking a review against the live PalomarPolicy rubric |
| `sandbox` | `bwrap` on `PATH` | running an engine inside a real Bubblewrap namespace |
| `codex` | `codex` on `PATH` | running pinned Codex through the real broker against a fake upstream |

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
for it. The end-to-end registration test runs instead against a committed,
synthetic database in public PalomarDatabaseTools CI. That contract contains
the real public schema and validators without any private ledger bytes.

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
pass crosses the window. A rebuild captures the live index's blob identity
before cloning the records and writes only against that identity. An admission
or pass that changes the queue between that capture and the conditional write
therefore makes the rebuild refuse its stale snapshot instead of overwriting
the live queue. The State repository runs the scheduled sweep in its own
concurrency group, so the command also reads back the exact blob it wrote and
fails loudly if another writer replaced or reserialized it. An ordinary pass
may continue over the queue it derived after a refused cache write, because the
concurrent live index remains intact for its next pass. A pass that cannot
enumerate its work fails; it never reports having found nothing.

The current rubric contract is schema version 8. The reviewer temporarily also
accepts version 7 so existing reviews and the separately deployed policy can
roll forward safely; new policy work targets version 8. It
rejects an internally inconsistent positive review: synthesis must reproduce
the evidence-pass scores exactly, acceptance
cannot override a failed pass, clean passes must meet the rubric minimum, and
mandatory floors cannot be softened. Non-blocking warnings on other dimensions
may accompany acceptance. These structural checks are the whole
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
the runner checks that the final AI-comment list reproduces every finding in
order. Positive checks, harmless edge cases, and excluded failure modes are
retained privately in `internal_notes` and removed from the served review. The
classification pass has an equivalent exact manifest for submitted codes.

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

The `palomar_reviewer.usage` module is the accounting boundary. It normalizes
engine evidence, builds the durable spend record, and computes an operator-facing
current-price summary from values supplied by the CLI. It has no filesystem,
subprocess, network, clock, or state-repository access. The CLI passes the
measurement time explicitly and owns persistence of the assembled accounting;
`palomar_reviewer.engine` derives the durable engine identity, collects the raw
events, and hands them to that pure accounting boundary.

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

`register` first reads the private delivered review and refuses anything but an
acceptance, before it clones a policy or source, downloads an artifact, or
writes a workspace. A real retry with a saved registration identity first
rechecks the current private consent and exact delivered-review/source binding,
then looks for that identity's deterministic Database branch and same-repository
open PR. An existing change is recovered at that boundary instead of rebuilding
the workspace or repeating archive and render side effects. It then checks the
exact reviewed policy and evidence when no recoverable change exists. No
submission is grandfathered past authorization: every registration requires
the private record to describe how push access was proved and not merely assert
that it was, to name no previous registration, to carry the submitter's
consent, and for the digest delivered, the digest consented to, and the review
about to be archived to be the same bytes. Only then does it dispatch the
pinned Challenge renderer, which is a public Actions run naming the repository
and commit and would otherwise signal an acceptance the submitter never agreed
to register.

The submission server records the proof method and its GitHub repository and
principal observations. The reviewer requires schema version 1, validates the
method's claimed binding, and binds the proof to the reviewed commit and
principal. It does not independently ask GitHub to resolve the stored
`repository_id` back to the submitted repository.

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
`scores/<id>-vN.json`, and not inside it. There is one current record schema:
the record is published exactly as committed, so the contract used here is the
same `schema-v2.json` served beside the data.
The render bundle is checked by the reviewer's own render validation and then by
the database's `tools/validate.py`. It archives the exact mechanical-report
bytes, the normalized run and job provenance, and the review itself in one
content-addressed evidence bundle together with `source-archive.json`, so a
single tree hash covers everything justifying the record; raw Actions logs are
deliberately not retained. It pushes a branch to
`PalomarRegistry/PalomarDatabase` and opens a PR.

Database registration requires Git 2.34 or newer. Its checkout begins
blob-filtered, depth one, and sparse: the filtered fetch itself registers
`origin` as the promisor and `blob:none` filter, then checkout lazily fetches
only the in-sparse schemas and tools. Historical `entries/`, `scores/`,
`renders/`, `evidence/`, and segmented registration projections are not
materialized. Registration reads the checked-out Git tree and fetches only the
exact submission/result/day authority blobs needed for the event; unrelated
authority and payload blobs remain promised. New files are normalized to mode
`100644`, enumerated rather than
added by directory, force-added by their explicit paths so an ignore rule
cannot omit one, and compared with the staged Git tree before the commit is
accepted.

The proposed commit is validated with the real `tools/validate.py --since` the
exact Database `main` commit it extends, so the validator hashes the new
immutable entry and bundles and proves the exact identity, result, submission, and day
projection transitions. Before invoking it, the reviewer asks the exact
checked-out `tools/validation_scope.py` owner to derive its scope; a fallback
stops with a sparse-checkout error instead of running an unscoped check over
historical records and bundles that are intentionally absent.

The checkout stays depth one through validation and for every dry run. Just
before a real push, the reviewer removes the shallow boundary with another
`blob:none` fetch. This is deliberately later and narrower than the old eager
unshallow, but cannot yet be deleted: registration previously reached GitHub
and was rejected because a no-workflow-scope credential pushing a shallow new
branch was treated as introducing an existing workflow. A scratch push with an
operator OAuth token proves the current shallow tree shape is accepted, but
that token has `workflow` scope and is not the production GitHub App token, so
it cannot disprove the permission-specific failure. The final fetch downloads
commit/tree history, not historical payload contents.

Registration identity reads only the immutable binding for this submission and
either this result's projection or this date's constant-size serial counter.
A source repository is stored there by its case-folded GitHub owner/name
comparison key; project and Comparator paths remain exact.
A first version reads identity/submission/day/result state and writes those four paths;
an update reads submission/result state and writes those two. The only growing
ordinary term is the touched result's ordered version list, which is capped at
500. No registry-sized JSON document is read, written, or staged, and the
explicit authority/path operation count is independent of total results.
Git's initial tree/index construction and later status/diff work still scale
with repository path count, even though historical blobs remain promised.
Registration also retains the filtered commit/tree history transfer at the
final push boundary; deleting that transfer requires an
experiment with the production App credential that proves GitHub no longer
applies the old workflow-scope interpretation.

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
to the organization's base Write role. Preservation refs carry submitter-authored
trees, workflow files included, so before anything is pushed to a fork the
reviewer reads that fork's own Actions setting. On the run that creates the
fork, while the account is still its administrator, the setting must read back
disabled or nothing is pushed, no ruleset is written, and the account is not
demoted. Later runs preserving into the same fork ask again, and an enabled
setting still refuses; but reading that setting needs the administrator grant
the creating run gave up, so GitHub answers those reads with a 403. That
specific 403 is announced as a warning and preservation continues, but only
once the fork's own metadata confirms the account no longer administers it:
an interrupted or hand-made lifecycle leaves the grant in place, and a 403
that the demotion cannot explain refuses like any other. Where the excuse does
hold, refusing would refuse every re-preservation for a state the reviewer
created itself. What covers the residual is the organization-wide Actions policy
(`enabled_repositories=none` on `PalomarArchive`) and the fact that only an
organization administrator can turn Actions on for a repository; giving the
reviewer a second credential with `Administration: read` would close it, at the
cost of another credential to hold. Any other failed read still refuses, and the
`PalomarRepairs` fork, whose account is never demoted, is checked strictly every
time. The account makes native forks, adds
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
The automatic loop records each registration attempt before starting it. A
render run with a concrete failure report pauses the registration immediately;
an unexplained operational failure waits 30 minutes and pauses after three
attempts. Paused work leaves the automatic queue, so it cannot keep the next
consented submission behind it. After correcting the cause, an operator uses
the State workflow's `retry_registration` input, or runs
`palomar-review retry-registration --submission ID` with explicit State-write
authority. That command revalidates the repository, commit, write proof,
delivered review, and existing consent before restoring the queue entry.
Full verification also records whether Mathlib's trusted cache client could
supply the pinned dependency closure. Review delivery copies that typed result
to private State, so the consent page can warn about a missing cache before the
submitter chooses registration; an older cache client with no recognizable
summary is reported as unknown rather than available or missing.
If the earlier process reached the Database branch, the retry fetches the exact
reserved entry from that branch before doing other work. It creates the missing
PR from a valid branch or, for an existing PR, requires the exact branch, base,
same Database repository, head commit, creation time, record identity, source,
review, and submission binding before checkpointing the PR in State. Fork PRs
that reuse the predictable branch name are ignored. A checkpoint already named
by State is validated before any replacement PR could be created. GitHub read
or authentication failure is not treated as branch absence. The recorded
creation time retains honest stale-change diagnostics, and a retry of an
already-recorded checkpoint is idempotent. A new attempt uses an ordinary
branch push and never force-replaces remote work; a branch that appears after
preflight is recovered on the next pass.
If another same-day registration merges first, the contiguous day counter
invalidates the saved serial and the attempt needs operator coordination; it
is never silently reallocated under a second identity.
Rerun `register`, or pass a previously downloaded trusted result with
`--render-result PATH`.

Permanent identifiers are `PALOMAR-YYYY-MM-DD-NNNNNN`. The date is the day the
result entered the registry, which is the day the submitter's consent was acted
on. `registrations/days/<date>.json` owns that day's last serial, so the next
identifier is exactly that counter plus one, starting at 1 for a date with no
counter.

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

Serials rise within a date, and a new version of an existing result reuses its
first version's identifier and with it that version's date rather than
allocating either, so ordering identifiers as strings is registration order
across the whole registry. The one thing that can put an older identifier in
late is the reservation above: a run that failed before midnight and is retried
after it finishes under the date it reserved, which is one identifier committed
late and no other identifier moved.

Once the PR is merged, verify the immutable record and close out the private
submission:

```bash
palomar-review finalize --submission a1b2c3d4e5f6 --pr 34
```

`finalize` refuses an unmerged PR, a PR without exactly one Palomar entry, or a
record that names a different submission.

## Engines

- `--engine codex`: runs `codex exec` ephemerally with its normal inspection
  and shell tools, a read-only Codex sandbox, and a JSON output schema. This is
  the production engine, and the only one behind the credential broker below.
  It requires an explicit `--model`, because the broker enforces the model it
  was configured for.
- `--engine claude`: runs `claude --print` in safe mode. Its explicit tool
  allowlist is empty for ordinary passes and contains only `WebSearch` and
  `WebFetch` for the literature pass; it has no filesystem or shell tool there.
  It binds a reusable Claude login into the model namespace and has no broker,
  so it is not a production engine and refuses to start unless
  `PALOMAR_ALLOW_UNBROKERED_CLAUDE=1` says the run is not one.
- `--engine command --command 'program ...'`: runs the named program, sends the
  prompt on stdin, and expects one JSON object on stdout. The program is not a
  tool-restricted model engine; its abilities are those of arbitrary code
  inside the outer namespace.

`palomar_reviewer.engine` is the execution boundary for all three: it validates
the selected configuration, constructs the exact provider command and
Bubblewrap namespace, collects subprocess output and Codex events, and
requires the complete stripped engine output to be one JSON object before
validating its schema. Prose and Markdown wrappers are not accepted. The CLI
owns rubric prompts and orchestration and checks every returned pass with the
credential-output backstop before that pass can enter another prompt or leave
the review workspace.

An acceptance-capable literature pass must be able to verify important sources
and search for obvious prior formalizations. Only the configured Claude
literature pass currently has explicit web research tools. This runner never
enables Codex web search and ignores user configuration that could enable it.
Codex, and any custom command without equivalent research access, therefore
must not award a literature score above the policy's verification ceiling.

Every engine is additionally launched inside a fail-closed Bubblewrap namespace.
The namespace exposes the submission at `/workspace`, a dedicated output
directory, and an empty scratch home. It does not expose the runner's GitHub
CLI configuration, registration credentials, unrelated home files, or other
workspaces. The engine transport can reach its model API in every pass. Claude
web tools are disabled, outside the literature/notability pass; this runner
never enables Codex web search and ignores Codex user configuration. General
host-level egress filtering would require a separate API-aware proxy.
`palomar-review doctor` refuses an installation without `bwrap`.

### The model credential broker

For the production Codex path, no reusable provider credential is inside that
namespace at all. `palomar_reviewer.broker` starts a short-lived child process
before Codex and shuts it down in a `finally`. That child holds the real
upstream key, binds one automatically allocated port on `127.0.0.1`, and serves
exactly one call: `POST /v1/responses`, authenticated by a random per-pass
capability compared in constant time and replaced with the real upstream
authorization on the way out. Codex is pointed at it by machine-level provider
overrides that the submitted repository cannot reach, and the capability
arrives in the namespace environment through a Bubblewrap `--args` pipe, so it
is in no process's command line either.

The broker serves the request pinned Codex makes and refuses the rest: another
method, path, model or reasoning effort; an unstreamed or background response;
any response not explicitly asked to go unstored, because the provider keeps
one it was not told to discard for at least thirty days; a body that names a
field twice, which two parsers could read two ways; priority processing; a
continued or provider-stored conversation; a
provider-hosted tool, which is how a namespace process would buy itself the web
research the policy says these passes do not have. It forwards only the headers
pinned Codex sends, returns only the ones it reads, follows no redirect, and
reaches no origin but the provider. It streams events through without buffering
an answer whole, and bounds the pass by request count, cumulative tokens,
estimated spend, request and response size, concurrency and open connections. A
request whose cost the provider never reported is charged a standing estimate,
so hiding usage exhausts the ceiling sooner rather than evading it. What it
counted, and what it refused, is recorded alongside the engine's own turn
evidence in `spend.json`. Neither credential is written to a log, an artifact,
an exception, or a returned header.

Two details are the difference between a boundary and the appearance of one.
The trusted reviewer, not the broker child, binds the loopback port and holds
it for the whole pass: a broker that dies leaves a port nothing else can take,
because a namespace process that knows the port and the capability could
otherwise listen there and answer a retry with a review nobody's model wrote.
And a pass refuses to start against a Codex other than the pinned release,
because the route, headers and request shape the broker enforces were read off
that release and are proven against it.

What this buys: a prompt injection that talks the model into reading and
transmitting everything it can reach comes away with a capability that only a
process on this runner can use, and only until the pass ends. Bubblewrap is
itself launched with an environment holding nothing worth reading, because its
own reaper is PID 1 inside the namespace and `/proc/1/environ` there is
whatever it was started with; `--clearenv` answers a different question. What it does not
buy: this is not network isolation. The namespace still shares the runner's
network, because the Codex transport has to reach the broker. It covers the
`codex:gpt-5.6-sol` launch path and no other provider; the Claude engine's own
login is still bound into its namespace, which is why that engine is not a
production engine. The output credential check remains as a backstop for the
cases the broker does not cover, and it catches a key copied out in plain text
rather than one that is encoded or sent another way.

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
spend.json                     # raw turn-aggregate usage evidence and broker counts; no historical USD value
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
files with Bubblewrap, keeps the provider key outside the namespace behind the
loopback broker, and validates strict output schemas. Codex still has shell
tools and a custom command is arbitrary code inside the namespace, so these
controls reduce accidental instruction following rather than preventing it;
what the broker adds is that succeeding at it wins nothing that outlives the
pass. While the private packet remains available, it lets an operator audit the
decision.

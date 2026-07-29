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

After inspecting that output, run and publish the editorial result:

```bash
palomar-review run --issue 12 --engine codex --model gpt-5.6-sol --apply
```

If the decision is `accept`, prepare the append-only database PR:

```bash
palomar-review publish --issue 12
```

`publish` validates the generated record against the database schema, pushes an
issue-specific branch to `kim-em/PalomarDatabase`, and opens a PR. It refuses
non-accept decisions and existing entry filenames.

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

Use `--policy-ref <40-char-sha>` to review against a specific policy commit.
Otherwise the tool resolves and records `kim-em/PalomarPolicy@main` at preparation
time.

## Audit trail

Each review directory retains:

```text
issue.json
mechanical-report.json
source/                  # detached source commit
policy/                  # detached policy commit
prompts/                 # fully rendered prompts
raw/                     # exact engine final messages
passes/                  # normalized per-pass JSON
review.json              # schema-validated final report
```

Raw session histories remain controlled by the chosen engine. Palomar records
the final messages, model identifier, policy commit, source commit, and public
issue report.

# CLAUDE.md — project rules for `x`

These rules apply to every session in this directory and override any
default or auto-mode guidance that says otherwise.

## 1. File editing: editor tools only

- Create, write, and edit files **only** with the `Write` and `Edit` tools.
- Never create or modify file contents through Bash: no `cat > file <<'EOF'`,
  no `echo >` / `>>`, no `sed -i`, no `tee`, no `awk` / `perl -i`, no
  `python - <<EOF` that rewrites a file.
- Read files with `Read`, not `cat` / `head` / `sed -n`.
- Bash is still used for searching, listing, running (tests, git, package
  managers, linters), and inspecting. Deleting files with `rm` requires
  showing the file and confirming with the user first.

## 2. Language

- All code, comments, identifiers, commit messages, config files, logs, and
  error messages are written in **English**.
- Document files (specs, plans, READMEs meant for humans under `docs/`) may
  be written in Indonesian when the user asks for it.
- Bahasa Indonesia is used only in the conversation with the user.

## 3. Plans from brainstorming sessions

- Plans written during a superpowers brainstorming session describe the
  **fundamentals and high-level technical approach only**: components,
  responsibilities, data flow, interfaces, key decisions, risks.
- Do **not** write full code blocks in plans. Short signatures or a few
  illustrative lines are fine when they clarify an interface.

## 4. Problems: always bring a solution

- When a problem, blocker, or failure appears, investigate it and present
  one or more concrete solutions (with a recommendation) to the user.
- Never just report a problem without proposing how to fix it.

## 5. Research before acting on third-party or unclear things

- Before using any third-party library, API, service, or undocumented
  behavior (e.g. X/Twitter internal GraphQL endpoints, headers, rate limits),
  research it first: official docs, source code of reference projects
  (twikit, sidecar, etc.), or live verification.
- Do not implement based on assumptions. State what was verified and what
  is still uncertain.

## 6. Planning structure

- **Spec:** the superpowers brainstorming skill produces **one single spec
  document** for the project, saved under `docs/superpowers/specs/`
  (`YYYY-MM-DD-<topic>-design.md`). Do not scatter the spec across many
  files; update that one file when the design changes.
- **Plans:** superpowers implementation plans are **split per phase**, one
  file per phase under `docs/superpowers/plans/`
  (e.g. `phase-0-live-verification.md`, `phase-1-foundation.md`). Each phase
  is independently deliverable and verifiable.

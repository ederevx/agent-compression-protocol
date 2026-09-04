# ACP v1.1 Evaluation — Hook-Enforced I/O Compression

**Status:** evaluation complete, per proposal §32's required deliverable format
**Verdict:** REVISE
**Evaluated:** 2026-09-03
**Repository:** `ederevx/agent-compression-protocol`
**Baseline:** `main` at `8d3bb82` (PR #2 merged)
**Proposal evaluated:** `agent_protocols_v1_1_hook_enforced_io_compression_proposal.md` (this directory)
**Implementation authorization:** granted for accepted items only, by explicit user direction after review of this evaluation — see "Disposition" below. REVISE/DEFER items still require live-probe evidence before any implementation.

---

## A. Baseline verification

- ACP `main` = `8d3bb82` (PR #2 merged), confirmed current at evaluation time.
- **Correction to the proposal's own starting assumption**: `acp/adapters/claude_code_bash_mcp.py`'s docstring claims `PostToolUse.updatedToolOutput` "does not exist" (per a claimed 2026-09-03 fetch of the hooks reference). This is inaccurate. The field is real, shipped in Claude Code v2.1.121, and documented. What's actually true is narrower and more specific: it is broken for built-in tools including Bash and WebFetch (see B below) — a different, more nuanced finding than "doesn't exist," and one that changes which fallback architecture is actually justified.
- **Unrelated operational finding, not part of this proposal's scope but discovered while establishing baseline**: both `~/.claude/settings.json` and `~/.codex/hooks.json` still wire `PreCompact` to `acp.adapters.hooks.precompact_pressure`, a module the pressure-subsystem-removal commit (`3a99722`, part of PR #2) deleted from disk. Every `PreCompact` event on both live hosts on this machine currently fails to locate that module (fails open, but should be cleaned up via reinstall regardless of this evaluation's outcome). Both live installs also carry a stale `SubagentStop` entry for `acp.adapters.hooks.subagent_report` with an outdated `statusMessage` ("best-effort background prewarm of a large transcript tail") describing behavior the same PR already removed from that module's code (it is now a no-op on `SubagentStop`, harmless but misleadingly labeled).

## B. Claude capability matrix

| Boundary | Docs claim | Evidence | Verdict |
|---|---|---|---|
| PostToolUse **Bash** replace | shipped v2.1.121, documented for all tools | Confirmed broken: open, reproducible bug `github.com/anthropics/claude-code#68951` (verified live via `gh issue view`) — hook fires successfully (exit 0, correct envelope), model still receives the original output | **REJECT** |
| PostToolUse **WebFetch** replace | same field | Same regression class, `#67442` (verified) | **REJECT** |
| PostToolUse **Read/Grep/Glob** replace | "all tools" since v2.1.121 | No issue confirms or denies specifically; untested | **DEFER** — live probe required |
| PostToolUse **MCP tool** replace | original pre-v2.1.121 scope (`updatedMCPToolOutput` generalized to `updatedToolOutput`, per closed doc issue `#54161`), narrower/older surface | Untested | **DEFER** — live probe required |
| PostToolUse **Agent** (C5, foreground child→parent report rewrite) | PostToolUse confirmed to fire for Agent tool completion (`#33049` clarifies only `Stop` doesn't fire for subagents, not `PostToolUse`) | Same "built-in tool" risk class as Bash/WebFetch — unverified | **REVISE** — treat as high-risk until proven otherwise, do not accept on documentation alone |
| PreToolUse **Agent `updatedInput`** (C4) | works generically per docs/community examples; one caveat (multiple simultaneous `updatedInput` hooks: last-registered-wins, not merged) | Well-corroborated, no counter-evidence found; resolves ACP's own pending uncertainty already flagged in `parent_child_context.py`'s docstring | **ACCEPT** |
| **SubagentStop** bounded retry (C6) | `decision:"block"` + `stop_hook_active` recursion guard + hard cap (8 consecutive blocks, `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` override); `last_assistant_message` field confirmed present | Mechanism structurally mirrors ADP's own production `Stop`-hook block/retry pattern (already proven live in this environment) | **REVISE** — mechanism is sound, but must confirm the parent has not already consumed the raw first-attempt report before the block takes effect (proposal's own explicit §12 rejection condition); not yet tested |
| **SendMessage** rewrite (C7) | `PreToolUse` can block/modify `SendMessage` input, including message text, via `updatedInput` | Confirmed, no counter-evidence found | **ACCEPT direction** — gate strictly on an explicit delimited support block, mirroring `parent_child_context.py`'s existing instruction/data separation discipline (proposal §22) |

Compounding reliability findings relevant to every "broken"/"unverified" row above, all real and open: malformed `updatedToolOutput` can crash the tool call outright instead of falling back to the original output (`#85631`) — directly at odds with the proposal's own hard fail-open requirement (§24); colliding hooks silently clobber each other's replacement, last-registered-wins (`#88338`); `Read`'s dedup cache does not account for prior substitutions, so a repeated `Read` of the same path can bypass a substituting hook entirely (`#88118`) — relevant if Read is ever accepted as a boundary.

## C. Codex capability matrix (proposal §16 producer table)

| Producer | Finding | Verdict |
|---|---|---|
| shell/Bash input rewrite | `updatedInput` currently *rejected* by Codex's own code outside `Bash`/`apply_patch`, per the current behavior described in open feature request `github.com/openai/codex#18491` (verified live) | Bash itself plausible; **REJECT** for everything else |
| `spawn_agent` input rewrite (D1) | No clean, corroborated confirmation found anywhere that `updatedInput` applies to `spawn_agent`; sources are contradictory; `#18491` is itself an open, unresolved request to add exactly this capability | **REJECT/DEFER** — do not assume parity with Claude's confirmed-working Agent case; matches ACP's own `deploy/codex/install.py` comment, which already flagged this as unconfirmed and remains unconfirmed today |
| PostToolUse output substitution (D2, `decision:block` vs `continue:false`) | Reported (single doc-fetch source) to replace the tool result post-hoc, not just append feedback | **No `PostToolUse` hook is installed anywhere in this machine's live Codex config** (`~/.codex/hooks.json`), so nothing here is locally verified; a stale trust-hash entry in `config.toml` suggests one existed and was removed previously | **DEFER** — needs a real live Codex session test before any weight is put on this |
| unified-exec / streaming (D5) | `exec_command`'s initial call matches as Bash and fires `PreToolUse`; `write_stdin`/poll continuation reported not to re-fire it; `PostToolUse` historically did not fire at all for the polling-completion path until a since-merged fix (`#16246`, closed via PR `#18888`) | Matches proposal's own D5 instruction directly: **explicit enforcement gap, record it, do not resurrect prefetch/pressure to compensate** |
| Hosted tools (WebSearch, etc.) (D6) | Confirmed (consistent across sources) to bypass local hook dispatch entirely, independent of hook capability | **REJECT** — permanent, documented gap |

A meaningful share of the Codex research traced to a single doc-fetch through an unfamiliar redirected domain (`developers.openai.com/codex/hooks` → `learn.chatgpt.com/docs/hooks`), and was contradicted in places by a second, source-grounded pass over the actual `openai/codex` repository. Anything above not also corroborated by a real, verified GitHub issue is being treated as unconfirmed, which covers most of Part II.

## D. ACP design impact

Only two boundaries clear this pass as low-risk and immediately actionable, both Claude-only, both requiring no new architecture:

- **C4** — flip on ACP's own already-written, already-fail-open `acp/adapters/hooks/parent_child_context.py` for Claude via `deploy/claude_code/install.py --with-parent-child-context`. Zero new code.
- **C7 direction** — a new `PreToolUse` hook on `SendMessage`, structurally identical to `parent_child_context.py` (explicit `<acp-context>` block, `downward_context` traffic class, fail-open), not yet written.

Everything else in Part I is REVISE/DEFER pending live probes; all of Part II (Codex) is DEFER pending a real live Codex session — the evidence base there is too thin and partly self-contradictory to inform design.

## E. Rejected/deferred surfaces

- **Rejected**: Claude native Bash/WebFetch output replacement (confirmed broken); Codex hosted-tool interception (confirmed bypassed); Codex `spawn_agent` input rewrite (contradicted/unconfirmed, leaning reject pending live evidence).
- **Deferred pending live probe**: Claude Read/Grep/Glob/MCP-tool output replacement; Claude Agent-result rewrite (C5); Claude SubagentStop delivery-timing question (C6); Codex PostToolUse output substitution generally (D2); Codex Bash-only input rewrite (D1 narrowed).
- **Explicit non-goal, not a failure**: Codex unified-exec streaming/poll gap (D5) — proposal already instructs recording this rather than solving it.

## F. Performance estimate

Not benchmarked this pass — out of scope for a documentation/issue-tracker-grounded evaluation. Proposal Part V (command-hook startup latency, persistent-MCP-hook comparison, token-savings-vs-overhead) remains required before any "v1.1-E" transport-optimization work, unchanged from the proposal's own instructions.

## G. Final verdict: REVISE

The proposal's direction — native host-hook enforcement over cooperative/producer-owned compression — is sound, and this evaluation found two genuine, low-risk wins independent of its riskier claims. But the proposal's own "priority: highest" boundary (native Bash output replacement on Claude) is rejected outright by a real, open, unfixed regression: the current MCP-owned-Bash architecture (which the proposal's own §9/C3 already planned to retain as a fallback) stays as the production path for Bash, not something being migrated away from. The Codex half of the proposal is substantially less certain than the Claude half and needs a live-session verification pass before it can inform any implementation decision, including the specific claim (`spawn_agent` `updatedInput`) ACP's own installer already flagged as unconfirmed and which remains unconfirmed today.

## H. Disposition and next stage

Per explicit user direction after reviewing this evaluation: proceed to (1) stage this work on a branch, (2) live-probe every DEFER-verdict boundary on both hosts before drawing a conclusion on it, (3) implement the boundaries that clear evaluation (starting from the two ACCEPT items above), (4) hold both hosts to parity — where a capability is confirmed on one host and not the other, that asymmetry is recorded explicitly in this document and in telemetry/capability state (proposal §25), never silently assumed. Results of each live probe, and the resulting implementation, are recorded as dated addenda below as they land.

### Live-probe and implementation log

*(populated as work proceeds)*

---

## I. Stop gate (evaluation phase — satisfied)

> ACP v1.1 evaluation complete. No implementation performed. Awaiting explicit user approval.

Approval received 2026-09-03; implementation phase begins per "Disposition" above.

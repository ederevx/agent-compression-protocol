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

**2026-09-03 — Claude-side live probes (all six DEFER items resolved)**

Tested via isolated project-scoped `.claude/settings.json` in scratch directories (no live session config touched), driven by non-interactive `claude -p --dangerously-skip-permissions` invocations, mirroring the reproduction pattern from issue #68951.

| Boundary | Prior verdict | Live-probe verdict | Evidence |
|---|---|---|---|
| PostToolUse **Read** replace | DEFER | **REJECT** | Hook fired (confirmed via log), model's final answer still quoted the real tool output, not the replacement marker — same failure class as Bash/WebFetch. |
| PostToolUse **Grep** replace | DEFER | **REJECT** | Same result: hook fired, replacement ignored, real match line returned. |
| PostToolUse **Glob** replace | DEFER | **REJECT** | Same result: hook fired, replacement ignored, real filename returned. |
| PostToolUse **MCP tool** replace | DEFER | **ACCEPT** | Hook fired and replacement worked: model received the marker value, not the MCP server's real output (model even flagged the mismatch). `updatedToolOutput` is honored for MCP-tool results specifically. |
| PostToolUse **Agent/Task** replace (C5) | REVISE | **REJECT** | Hook fired, but parent's final answer quoted the subagent's real report, not the replacement — same broken-for-built-in-tools class as Bash/Read/Grep/Glob. |
| **SubagentStop** bounded retry (C6) | REVISE | **ACCEPT** | Clean result: `decision:"block"` (top-level field, not under `hookSpecificOutput`) forced one retry; parent only ever saw the subagent's final, post-retry report — the pre-block value never leaked. `stop_hook_active` correctly `false` on the first block, `true` on the second, confirming the recursion guard. |

**Net conclusion**: `updatedToolOutput` is reliable *only* for MCP-tool output and does not extend to any tested built-in tool (Bash, WebFetch, Read, Grep, Glob) or to the Agent/Task tool's own result. `SubagentStop`'s block/retry mechanism is confirmed sound and safe to build on. This materially changes the v1.1 design: Claude-side native output-replacement work should target (a) new/existing MCP-tool output only, and (b) SubagentStop-driven enforced retry for subagent reports — not a general PostToolUse rewrite layer.

**Revised Claude-side implementation set**, superseding §D's original two items:
- **C4** — enable `--with-parent-child-context` (ACCEPT, unchanged).
- **C6** — replace/augment the cooperative `SubagentStart`-injection ask for subagent report compression (`subagent_report.py`) with an enforced `SubagentStop` block/retry: if a subagent's final report exceeds a size threshold and the `report` tool was not called, block once with a reason instructing the subagent to call `report`, then allow through on retry (respecting the 8-block hard cap). This is a genuine reliability upgrade over "ask nicely via injected context," now empirically justified.
- **C7** — SendMessage rewrite via `PreToolUse.updatedInput` (ACCEPT direction, unchanged, not yet built).
- **New, not in original proposal scope**: MCP-tool `PostToolUse` output enforcement is now a confirmed-viable mechanism; worth evaluating as a fail-closed backstop for `acp-bash`'s own output (currently fail-open if the MCP server's own compression logic fails) in a later pass — flagged here, not in this implementation round's scope.

**2026-09-03 — Codex-side live probes (D1/D2/D4 resolved)**

Tested via an isolated `CODEX_HOME` (own `hooks.json`, live `~/.codex/hooks.json`/`config.toml` never touched) driven by `codex exec --dangerously-bypass-hook-trust --dangerously-bypass-approvals-and-sandbox` against a real, reachable backend. Schema ground truth pulled directly from the installed `codex` binary (`strings`), not docs: Codex's compiled `HookEventName` enum includes `subagent_stop`, and hook output fields include `updatedInput` and `updatedMCPToolOutput` (narrower/MCP-scoped, unlike Claude's generalized `updatedToolOutput`) — but there is **no `SubagentStopHookSpecificOutputWire`** in the binary's schema at all, unlike `SubagentStart`.

| Boundary | Verdict | Evidence |
|---|---|---|
| **D1** — `spawn_agent` input rewrite via `PreToolUse.updatedInput` | **REJECT** | Live `spawn_agent` call observed as `collaborationspawn_agent`/`collaborationwait_agent` (namespaced), with `message` arriving as an opaque encrypted blob — not human/hook-editable content regardless of mechanism. The hook's `updatedInput` mutation attempt (even an unmodified re-submission) was rejected outright (`hook: PreToolUse Failed`) for both calls, matching open feature request `openai/codex#18491`'s claim that only `Bash`/`apply_patch` are currently supported. |
| **D2** — `PostToolUse` output substitution | **REJECT** (both built-in and MCP) | Against `Bash`: hook fired correctly, `updatedMCPToolOutput` mutation produced `hook: PostToolUse Failed`, original output reached the model. Against a real registered MCP tool (`echo-mcp`, a genuine stdio server): `PostToolUse` fired and captured the correct real response, but the same substitution attempt again produced `hook: PostToolUse Failed` and the model's final answer still quoted the original, unreplaced tool output. Unlike Claude (where the MCP-scoped case works), Codex CLI 0.153.2 has no working output-substitution path at all, built-in or MCP. |
| **D4** — SubagentStop-equivalent + report-text field | **REJECT** (no block/retry decision possible), field confirmed present | `subagent_stop` is a real event and fires correctly; `last_assistant_message` is confirmed present and accurately populated (verified directly: matched the subagent's real reply text), corroborating issue `openai/codex#23784`. But since no `SubagentStopHookSpecificOutputWire` exists in the compiled schema, there is no mechanism to return a block/retry decision from this event on Codex — it can only be observed, never enforced. |

**Conclusion — parity is not achievable at the mechanism level for this proposal, on Codex CLI 0.153.2.** Every boundary accepted for Claude (MCP-tool `PostToolUse` substitution, `SubagentStop` bounded retry) is confirmed unavailable on Codex; even Claude's other accepted mechanism family (`PreToolUse.updatedInput`, which C4 and C7 both rely on) is confirmed rejected for Codex's `spawn_agent` equivalent. Per this document's own "Disposition" principle (§H): this asymmetry is recorded explicitly here rather than silently assumed away or faked with a no-op equivalent. ACP's Codex adapter should remain on its existing, already-working cooperative/producer-owned architecture (`SubagentStart` injection ask for large reports, MCP-owned Bash) unchanged — it is not being regressed, and it is not being extended to match Claude's new capabilities, because Codex's current hook implementation has no mechanism to support them. Revisit if a future Codex CLI version adds a `SubagentStop` output wire or extends `updatedInput`/`updatedMCPToolOutput` reliability (tracked upstream by `openai/codex#18491`).

**C4 reconciliation**: this evaluation's baseline review noted `~/.claude/settings.json` already had the `PreToolUse/Task` `parent_child_context` hook live-installed despite `deploy/claude_code/install.py` documenting `--with-parent-child-context` as opt-in and "unconfirmed" at the time. No shell-history record of when the flag was passed was found, but the hook has been running live in production without incident, and this evaluation's research found no counter-evidence for Claude's Task-tool `updatedInput` support. Treated as sufficiently confirmed for Claude; `install.py`'s docstring is being updated separately (implementation pass) to reflect confirmed status rather than "unconfirmed."

---

## I. Stop gate (evaluation phase — satisfied)

> ACP v1.1 evaluation complete. No implementation performed. Awaiting explicit user approval.

Approval received 2026-09-03; implementation phase begins per "Disposition" above.

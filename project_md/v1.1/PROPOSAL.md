# ACP v1.1 Proposal — Hook-Enforced I/O Compression

**Status:** proposal for implementer evaluation  
**Implementation authorization:** **NOT GRANTED** by this document  
**Target:** ACP v1.1 candidate  
**Prepared:** 2026-09-03  
**Repository:** `ederevx/agent-compression-protocol`  
**Baseline:** current `main`, with merged PR #2 as the latest architectural change considered  
**Evaluation owner:** next implementer agent

---

## 1. Purpose

Evaluate whether ACP v1.1 should move its primary Claude Code and Codex integration from producer ownership / cooperative reporting toward **native host hook-enforced I/O compression**, while retaining the simplified synchronous ACP core established by the latest merged PR.

This document is intentionally an **evaluation proposal**, not an implementation plan that may be executed automatically.

The implementer MUST:

1. re-audit current Claude Code and Codex hook semantics from authoritative current sources and the installed/live host where available;
2. test the claimed replacement boundaries rather than trusting documentation alone;
3. compare those findings against the current ACP adapter code;
4. return one of:
   - `ACCEPT` — substantially implementable as proposed;
   - `REVISE` — direction is sound but concrete surfaces/design need correction;
   - `REJECT` — enforcement semantics, reliability, complexity, or performance make the proposal unsuitable;
5. identify the smallest safe implementation sequence if accepted/revised;
6. **STOP after evaluation and wait for explicit user approval before modifying ACP behavior.**

---

## 2. Starting point: ACP after latest merged PR

The evaluation starts from the current simplified architecture, not from earlier Phase 4 assumptions.

The latest merged ACP PR removed dead or non-actionable machinery including:

- pressure tracking and `context.pressure`;
- pressure-triggered maintenance;
- PreCompact pressure reporting;
- background `context.prepare` / `context.resolve` prefetch;
- SubagentStop transcript-tail prewarming;
- non-functional urgency state;
- telemetry tied to those dead paths;
- provenance/failure-policy complexity that no longer affected a real host replacement path.

The important retained primitive is:

```text
host adapter
    -> ACP context.evaluate
        -> deterministic gate
        -> PASS / COMPACT / COMPRESS
        -> external AALP compressor when inspection is required
```

### Baseline invariants

Unless the implementer proves a concrete need otherwise, preserve these:

1. Do not resurrect pressure tracking only to observe approaching native compaction.
2. Do not add background compression unless there is a proven later boundary that installs the prepared result instead of the raw content.
3. Do not introduce an outbound Claude/Codex API proxy solely to rewrite model context.
4. Compression remains external-only; no silent native Claude/Codex summarization fallback.
5. Ordinary host integration remains fail-open when ACP/AALP is unavailable or invalid.
6. Prefer compression **before the receiving model consumes the payload**.
7. Keep `context.evaluate` as the single real ACP compression decision path unless evaluation proves that interface cannot support a required host boundary.

---

## 3. Why v1.1 is worth evaluating

Current ACP integration still reflects older assumptions that may no longer be true for current hosts:

### Existing assumption A — ACP must own Bash

Current ACP can deny the native Bash tool and expose an ACP-owned MCP `bash` tool so command output passes through `context.evaluate` before Claude/Codex receives it.

This is reliable when enforced, but it only protects the producers ACP owns or replaces.

Current Claude Code hook semantics appear to offer a stronger path: successful native tool output may be replaceable in `PostToolUse`, allowing ACP to compress the native result while preserving the native tool itself.

### Existing assumption B — child reports require cooperation

Current `SubagentStart` injects instructions asking the child to call ACP's `report` MCP tool before returning a large final report.

Current host surfaces appear to offer stronger interception in at least some paths:

- foreground Claude Agent completion appears as a normal tool result that may itself be rewritten;
- current SubagentStop events expose enough final-message/control information to consider bounded retry enforcement for completion paths that cannot be rewritten directly.

### v1.1 hypothesis

The candidate architecture is:

```text
native producer
     |
     v
host mutation/replacement hook
     |
     v
ACP context.evaluate
     |
     +-- PASS ----------> preserve original representation
     |
     +-- COMPACT -------> install reduced representation
     |
     +-- COMPRESS ------> install reduced representation
     |
     v
receiving model/context
```

The key word is **install**.

Observation-only hooks do not count as compression boundaries.

---

## 4. Definition of an enforceable ACP boundary

A host surface may be called an ACP v1.1 compression boundary only when all are proven:

1. ACP sees the complete candidate payload before the next receiving model turn consumes it.
2. ACP can synchronously decide PASS/COMPACT/COMPRESS.
3. The host accepts ACP's replacement representation.
4. The raw original representation is absent from the receiving model's effective context at that boundary.
5. Required tool/result metadata and success/failure semantics remain intact.
6. ACP failure can fall back to the original payload without breaking normal host operation.
7. No uncontrolled recursion or retry loop is introduced.

If the hook merely appends context while the original result remains present, it is **not** an I/O compression boundary.

If a hook runs but the host silently ignores its replacement field, it is **not** an I/O compression boundary.

---

## 5. Candidate v1.1 scope

Evaluate the following as the intended v1.1 surface.

### Claude Code

- successful native/MCP tool output via `PostToolUse` replacement;
- parent -> child support context via `PreToolUse.updatedInput` on the current Agent/subagent tool;
- foreground child -> parent final report by rewriting the Agent tool result;
- agent/team message payload before delivery when represented as a hookable local tool call;
- bounded SubagentStop retry only for completion paths not directly replaceable;
- runtime capability probes for replacement behavior;
- retain ACP MCP Bash/report as fallback until native paths prove sufficiently reliable.

### Codex

- parent -> child support context via `PreToolUse.updatedInput` on `spawn_agent` / current equivalent;
- successful local-tool output substitution through current PostToolUse continuation/feedback behavior when it can be proven that the raw result is removed from the next model turn;
- bounded SubagentStop retry where needed;
- explicit documentation of hosted/specialized tool paths that bypass hooks;
- retain producer-owned ACP tools where hooks cannot guarantee replacement.

### Shared optimization candidate

- evaluate persistent MCP hook handlers / ACP host-bridge calls for high-frequency hooks so ACP does not spawn a new Python interpreter for every small tool result.

---

## 6. Explicit v1.1 non-goals

Do not implement these merely to increase nominal coverage:

- pressure subsystem restoration;
- proactive/background compression without an install boundary;
- full transcript reclamation after raw content has already entered the context;
- native compaction replacement;
- arbitrary user-prompt rewriting;
- arbitrary summarization of mixed instruction + context payloads;
- transparent outbound API proxying;
- pretending hosted/streaming channels are covered when the host bypasses hooks;
- destructive rewriting of exact code/source/protocol data whose fidelity is required by the next operation.

---

# Part I — Claude Code evaluation

## 7. C1 — Native `PostToolUse` output compression

**Priority: highest.**

Evaluate whether current Claude Code reliably applies `hookSpecificOutput.updatedToolOutput` to the actual model-visible result for:

- Bash / PowerShell;
- Read;
- Grep;
- Glob;
- WebFetch;
- WebSearch if hookable in the current version;
- Agent;
- MCP tools;
- other retrieval/search/file tools capable of returning large text.

### Mandatory rule: preserve native result schema

Do not assume all tools can be replaced with a bare string.

For every supported tool, record:

```text
tool name
input schema
PostToolUse tool_response schema
compressible field(s)
non-compressible metadata fields
required replacement schema
```

ACP should transform only the model-visible text-bearing portion while preserving:

- status;
- tool success/failure meaning;
- IDs;
- timing;
- model usage;
- error flags;
- file/path metadata;
- structured values required by the host.

### Acceptance proof

Hook stdout is not sufficient.

The probe must demonstrate that the **next model-visible/transcript tool result contains the ACP replacement and not the raw sentinel**.

### Known regression risk

There is an open Claude Code bug report (#68951) documenting versions where `updatedToolOutput` hooks executed but built-in Bash still delivered the original output to the model.

Therefore v1.1 MUST evaluate runtime behavior and MUST NOT gate exclusively on a version number or documentation claim.

---

## 8. C2 — Runtime capability probing

Evaluate a capability-probe layer instead of static host assumptions.

Candidate capabilities:

```text
claude.posttooluse_replace_builtin
claude.posttooluse_replace_mcp
claude.pretooluse_updated_input_agent
claude.agent_result_replace
claude.subagent_stop_last_message
claude.subagent_stop_retry
claude.sendmessage_input_rewrite
```

### Desired probe properties

- harmless and deterministic;
- verifies the exact field/mechanism ACP will use in production;
- observes model-visible/transcript state rather than only hook execution;
- stores a bounded capability result;
- can invalidate or re-probe after host upgrades;
- never requires real sensitive input.

The implementer must recommend whether each capability is:

- install-time probed;
- session-time probed;
- cached with host-version fingerprint;
- or too expensive to probe automatically and therefore exposed as an explicit diagnostic.

---

## 9. C3 — Native Bash first, MCP Bash fallback

Do not remove current ACP Bash ownership in the first v1.1 change.

Evaluate this migration policy:

```text
native PostToolUse replacement proven
    -> native Bash remains available
    -> ACP compresses its result through PostToolUse

native replacement broken/unproven
    -> keep current permissions deny for native Bash
    -> ACP MCP bash remains exclusive producer-owned path
```

Only after real-host acceptance should another change decide whether the MCP Bash compatibility path may be deprecated.

---

## 10. C4 — Parent -> child support-context enforcement

Current `parent_child_context.py` has an important safety invariant worth preserving:

> Only compress explicitly delimited supporting context, never guess which arbitrary prompt text is instruction versus supporting material.

Continue using an explicit representation such as:

```text
<acp-context>
large supporting material
</acp-context>
```

The implementer must re-audit current Claude subagent tool naming and input schema.

Earlier ACP integration refers to `Task`; current Claude may use `Agent` as the real subagent tool.

Candidate flow:

```text
Agent.prompt
    instruction text
    <acp-context>...</acp-context>
           |
           v
    ACP downward_context
           |
           v
replace only support block
```

### Acceptance

Prove in child-visible state/transcript that:

- instruction text is unchanged;
- ACP-compressed support context is present;
- raw oversized support block is absent.

---

## 11. C5 — Foreground child -> parent via Agent result rewrite

Evaluate whether foreground Agent completion is returned through a normal structured tool result containing the child's final message.

If yes, prefer direct PostToolUse transformation over cooperative `report`.

Candidate transform:

```text
Agent tool_response
    status                  preserve
    agent id                preserve
    resolved model          preserve
    usage/timing            preserve
    content[].text          ACP native_agent_report
```

### Acceptance

Prove that:

- parent receives the compressed final report;
- raw child final report is absent from parent-visible Agent result;
- agent metadata remains valid;
- foreground completion remains a successful Agent tool call;
- async/background launch results are not mistaken for final reports.

If this is reliable, the implementer should recommend whether cooperative `report` remains necessary for Claude foreground subagents.

---

## 12. C6 — Bounded SubagentStop fallback

For background or otherwise non-replaceable completion paths, evaluate one bounded retry:

```text
SubagentStop
    |
    +-- final small / ACP PASS -> allow stop
    |
    +-- ACP reduced output
            -> reject stop once
            -> instruct child to return exact reduced representation
            -> next SubagentStop must not reject again
```

Use current recursion protection such as `stop_hook_active` or its actual current equivalent.

### Hard requirements

- at most one ACP-driven stop retry per completion;
- ACP failure -> allow original stop;
- no hidden infinite loop;
- do not use this path when direct Agent-result rewrite is available;
- verify the raw first final report is not already delivered to the parent before the replacement retry.

If the raw first report is already consumed by the parent, reject this as a token-reduction mechanism.

---

## 13. C7 — Agent/team message compression

Evaluate current direct agent/team messaging tools, e.g. `SendMessage` or current equivalent.

A PreToolUse boundary is attractive because ACP can reduce support material before another agent context receives it.

Safety rules:

- preserve recipient/routing/control fields exactly;
- do not summarize arbitrary instruction text;
- compress only a dedicated payload/context field or explicit `<acp-context>` block unless the current host schema provides a reliable instruction/data separation;
- verify receiver sees only the transformed support material.

---

# Part II — Codex evaluation

## 14. D1 — Parent -> child via `PreToolUse.updatedInput`

Re-audit current Codex hook handling for `spawn_agent` / Agent matcher aliases.

Evaluate whether ACP can rewrite the prompt/input before child creation using the same explicit support-context invariant used on Claude.

### Acceptance

The child must receive the ACP-modified input and not the raw context block.

---

## 15. D2 — Local tool-output substitution

Codex does not necessarily expose the same typed `updatedToolOutput` field as Claude.

Evaluate current documented/runtime PostToolUse semantics for replacing the model-visible tool result with hook feedback.

Specifically compare:

```text
decision: "block"
```

against:

```text
continue: false
```

or the current equivalents.

The preferred mechanism should:

- hide the raw result from the next model turn;
- preserve the semantic success of a successfully completed tool;
- not cause nested/code-mode tool promises to reject merely because ACP compressed output;
- allow fail-open raw passthrough when ACP fails.

### Rejection condition

If the host keeps the raw result in context and merely appends ACP feedback, the path does not save context and must not be treated as ACP compression.

---

## 16. D3 — Codex coverage matrix

Codex coverage must be measured producer by producer.

The implementer must fill this table from current authoritative/runtime evidence:

| Producer | Hook fires | Input mutable | Output substitutable | Raw hidden from model | ACP v1.1 support |
|---|---:|---:|---:|---:|---|
| shell / Bash | | | | | |
| unified exec | | | | | |
| MCP tool | | | | | |
| local function tool | | | | | |
| spawn_agent | | | | | |
| hosted WebSearch | | | | | |
| other hosted tools | | | | | |
| specialized paths | | | | | |

Do not claim host-wide Codex enforcement if hosted or specialized producers bypass hooks.

---

## 17. D4 — Codex SubagentStop fallback

Evaluate the same bounded one-retry pattern only for completion paths not directly substitutable.

Again, prove the original child final report has not already been installed in the parent's effective context.

---

## 18. D5 — Unified-exec / streaming gap

Determine whether long-running unified-exec processes can expose intermediate `write_stdin`/poll output to the model without a new Pre/PostToolUse boundary.

If yes:

- record it as an explicit ACP v1.1 enforcement gap;
- do not resurrect pressure/prefetch to compensate;
- separately evaluate producer ownership/wrapping only if the user later authorizes it.

---

## 19. D6 — Hosted-tool gap

Identify all current Codex hosted tools that bypass local lifecycle hooks.

The v1.1 implementation/documentation must say exactly which producers ACP can enforce and which remain outside the hook interception plane.

---

# Part III — Shared architecture

## 20. H1 — High-frequency hook transport

If ACP attaches to every tool result, process startup overhead becomes important.

Evaluate two shapes:

### A. command hook shim

```text
host
 -> launch Python hook
 -> AcpClient over ACP Unix socket
 -> evaluate
 -> hook response
```

Advantages:

- simplest;
- mirrors existing adapters;
- easy failure isolation.

Costs:

- Python startup on every tool call;
- more process churn.

### B. persistent MCP hook handler / ACP host bridge

```text
host
 -> already-connected ACP MCP server
 -> hook-facing evaluate operation
 -> ACP Unix socket / current interface
 -> hook response
```

Evaluate only if current hosts guarantee the necessary semantics.

Required properties:

- synchronous when replacement is required;
- no recursive hook invocation;
- no permission prompt for internal hook RPC;
- no bypass of ACP's public interface boundary;
- measurable latency win over command process startup.

Do not redesign ACP around MCP hook handlers if either host's implementation remains experimental or inconsistent.

---

## 21. Traffic-class policy

Before creating any new traffic class, evaluate whether current classes already express the needed semantics:

- `general`
- `native_agent_report`
- `downward_context`

Likely mapping to test:

```text
tool -> same agent       native_agent_report or general
child -> parent          native_agent_report
parent -> child support  downward_context
agent -> agent support   downward_context or separately justified class
```

A new class is justified only if its thresholds or semantic compression policy genuinely need to differ.

---

## 22. Instruction/data separation

ACP must never blindly summarize a mixed instruction + context payload.

For downward/message inputs, accept one of:

1. explicit `<acp-context>` support block;
2. a host schema that unambiguously separates the executable instruction from bulk supporting content;
3. another explicit structured convention approved by the user.

Do not infer instruction boundaries heuristically from arbitrary prose.

---

## 23. Exact-result semantics

Large does not automatically mean compressible.

The adapter/compressor must be conservative with payloads such as:

- exact source code later patched by line/offset;
- exact structured protocol messages;
- exact logs when subsequent steps depend on precise line identity;
- serialized machine-readable output consumed by another tool;
- binary/base64-like data;
- results whose tool-specific metadata is semantically required.

ACP's PASS outcome is valid and must remain respected.

---

## 24. Failure behavior

Default rule:

```text
original host I/O available
       |
       v
ACP/AALP error, timeout, invalid response, hook bug
       |
       v
original host I/O proceeds unchanged
```

Do not transform a successful host tool into a failure just because compression failed.

Codex requires special scrutiny because some hook stop/block mechanisms may reject an otherwise successful nested tool promise.

---

## 25. Capability state

The implementer should recommend a bounded representation rather than scattered hard-coded assumptions.

Illustrative only:

```text
HostCapabilities
  claude:
    pretooluse_agent_input
    posttooluse_builtin_replace
    posttooluse_mcp_replace
    agent_result_replace
    subagent_stop_final_message
    subagent_stop_retry
    message_input_rewrite

  codex:
    pretooluse_agent_input
    local_tool_result_substitution
    substitution_preserves_success
    subagent_stop_final_message
    subagent_stop_retry
    hosted_tool_hook_coverage
```

The evaluator must recommend when/how this state is refreshed after host upgrades.

---

## 26. Telemetry proposal

PR #2 removed counters that represented inert mechanisms. Do not replace them with another large speculative telemetry set.

Only add counters for real boundaries.

Candidate minimum:

- hook evaluation attempts;
- hook PASS/bypass;
- hook replacement emitted;
- replacement capability disabled;
- fail-open events;
- estimated input tokens at boundary;
- estimated/model-reported output tokens after reduction;
- direction-specific savings:
  - tool -> agent;
  - parent -> child;
  - child -> parent;
  - agent -> agent.

If ACP cannot verify that a host actually installed a replacement, do not count an emitted hook response as guaranteed tokens saved.

---

# Part IV — Required evidence and tests

## 27. Claude acceptance probes

At minimum, the implementer evaluation should define or run probes for:

1. Bash large result: raw sentinel absent, ACP replacement present in next model-visible/transcript result.
2. Structured Bash result metadata preserved.
3. Read result replacement.
4. Grep/Glob result replacement.
5. MCP result replacement.
6. Foreground Agent final report replacement.
7. Parent -> child Agent prompt context rewrite.
8. ACP unavailable: original result passes and tool remains successful.
9. Simulated/real `updatedToolOutput` regression: ACP detects inability and chooses fallback.
10. SubagentStop fallback cannot loop.
11. If WebFetch/WebSearch are claimed supported, prove them separately.
12. Agent/team message rewrite if claimed supported.

---

## 28. Codex acceptance probes

At minimum:

1. local shell result substitution with raw result absent from next model turn;
2. selected substitution mechanism preserves successful code-mode/nested promise semantics;
3. MCP result substitution;
4. `spawn_agent` input rewrite reaches child;
5. bounded SubagentStop retry;
6. ACP unavailable -> raw result continues normally;
7. hosted-tool bypass demonstrated explicitly;
8. unified-exec streaming/poll behavior measured explicitly.

---

## 29. Existing ACP regression requirements

All current ACP tests for the simplified synchronous core must remain green.

Host-adapter work must not:

- import or instantiate `Coordinator` from a host hook process;
- bypass ACP's authenticated public interface;
- reintroduce removed prepare/pressure APIs;
- weaken AALP separation;
- silently change failure behavior from passthrough to blocking.

---

# Part V — Performance gate

## 30. Benchmark requirements before enabling broad hooks by default

Measure:

- command-hook startup latency;
- persistent MCP-hook latency if evaluated;
- ACP BYPASS path latency;
- ACP real compression latency;
- failure-path latency when ACP/AALP is unavailable;
- token savings on representative:
  - Bash/log output;
  - Read;
  - Grep/Glob;
  - retrieval/WebFetch-like results;
  - foreground Agent reports;
  - downward support context;
- time-to-next-model-turn delta.

Primary decision question:

> Is broad interception economically beneficial after accounting for hook/transport latency on the many tool calls that ACP immediately bypasses?

The deterministic gate makes compression decisions cheap after ACP receives a payload; it does not eliminate host hook/process overhead.

---

# Part VI — Proposed implementation sequencing if approved

## 31. Evaluation must recommend a minimal staged rollout

Suggested sequence for consideration, not authorization:

### v1.1-A — Host conformance probes only

- capture current host schemas;
- prove real replacement semantics;
- encode capability checks;
- no default behavior migration yet.

### v1.1-B — Claude native tool-output path

- typed PostToolUse result adapters;
- retain MCP Bash fallback;
- instrument actual installed replacements.

### v1.1-C — Claude Agent I/O

- current Agent parent -> child context rewrite;
- foreground child -> parent result rewrite;
- bounded fallback only where direct result replacement cannot apply;
- then reconsider cooperative `report` requirement.

### v1.1-D — Codex enforceable local boundaries

- `spawn_agent` input rewrite;
- verified local-tool result substitution;
- explicit hosted/streaming gaps.

### v1.1-E — Hook transport optimization

Only if benchmarks justify it:

- persistent MCP hook handler / host bridge;
- eliminate per-event interpreter startup where safe.

### v1.1-F — Compatibility cleanup

Only after live acceptance:

- decide whether native Bash deny can be removed by default on Claude;
- decide whether MCP `bash` remains fallback-only;
- decide whether cooperative `report` remains necessary for any host path;
- remove only mechanisms proven redundant.

The implementer should revise this sequence if evidence shows a smaller or safer ordering.

---

# Part VII — Required implementer evaluation output

## 32. Deliverable format

Before any code changes, return a report containing:

### A. Baseline verification

- current ACP HEAD inspected;
- latest merged PR identified;
- current host adapter assumptions summarized;
- no use of stale project assumptions without re-verification.

### B. Claude capability matrix

| Boundary | Docs claim | Live probe | Replace before model? | Raw absent? | Semantic risk | Recommendation |
|---|---|---|---:|---:|---|---|
| PostToolUse Bash | | | | | | |
| PostToolUse Read | | | | | | |
| PostToolUse Grep/Glob | | | | | | |
| PostToolUse MCP | | | | | | |
| PostToolUse Agent | | | | | | |
| PreToolUse Agent | | | | | | |
| SubagentStop | | | | | | |
| SendMessage/current equivalent | | | | | | |

### C. Codex capability matrix

Use the producer matrix from Section 16.

### D. ACP design impact

For every accepted boundary:

- exact adapter file(s) likely affected;
- whether `context.evaluate` is sufficient unchanged;
- traffic class;
- replacement schema handling;
- fail-open mechanism;
- capability probe requirement;
- expected telemetry;
- test strategy.

### E. Rejected/deferred surfaces

List every hook/channel that cannot actually remove raw content from the receiving model context.

### F. Performance estimate/probe

Identify expected per-call overhead and where persistent hook transport is justified.

### G. Final verdict

One of:

```text
ACCEPT
REVISE
REJECT
```

with rationale.

### H. Proposed minimal first implementation PR

Only describe it. Do not implement it.

### I. Stop gate

End the evaluation with:

```text
ACP v1.1 evaluation complete. No implementation performed. Awaiting explicit user approval.
```

---

# 33. Decision principles for the implementer

When evidence conflicts with this proposal:

1. current live host behavior wins;
2. current authoritative host documentation is next;
3. current ACP code/contract wins over historical project assumptions;
4. this proposal is a hypothesis to test, not an authority that overrides evidence.

Prefer a smaller set of **proven hard replacement boundaries** over a broad architecture that merely observes or duplicates raw context.

The goal of ACP v1.1 is not to maximize the number of installed hooks.

The goal is to maximize the amount of expensive I/O that ACP can **actually replace before model consumption**, while preserving native host semantics and the simplified ACP core.

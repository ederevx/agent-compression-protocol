# agent-compression-protocol (ACP)

A pervasive, host-wide context-compression plane for frontier agents and
their native subagents on Claude and Codex. ACP intercepts large tool
output, file/search/retrieval results, native-agent reports, and other
oversized context at every safe host interception point, and reduces it
via PASS / COMPACT / COMPRESS decisions — external-only, with no silent
fallback to native compaction on compressor failure. Raw authoritative
source stays recoverable through source references and out of Git.

ACP is one of three protocols defined in `agent_protocols_v1`:

- **ADP** (`agent-delegation-protocol`) — native-only delegation
  enforcement.
- **ACP** (this repository) — host-wide context compression.
- **AALP** (`agent-api-lane-protocol`) — the compression-only external API
  transport ACP compresses through.

See the `agent_protocols_v1` project metadata for the full architecture
and implementation phases.

## Running ACP

Run ACP as a standalone process with `python -m acp` (see `acp/serve.py`);
it constructs `acp.coordinator.Coordinator` from `--aalp-root`/`--root`
and starts `acp.ingress.Ingress` on it over a Unix domain socket,
publishing `.acp/state/ingress.json` + `.acp/state/ingress.secret` for a
client to bootstrap against (see `interface/v1/README.md`'s Bootstrap
section). `--aalp-root` (or the `AALP_HOME` environment variable) is
required -- it points at the AALP instance ACP compresses through, and
ACP never guesses a sibling directory for it. `--root` and
`--socket-path` override the `ACP_HOME`-derived/default values.

To run ACP as a supervised, boot-surviving service instead of a foreground
process, see `deploy/install.sh` (installs `deploy/acp.service` as a
`systemd --user` unit, then registers the Claude Code host adapter below
unless run with `--no-claude-code`).

## Claude Code and Codex host adapters

`acp/adapters/claude_code_bash_mcp.py` is a host-neutral stdio MCP server
(despite the name -- it predates the Codex adapter) exposing two tools:

- `bash`: runs a shell command and routes its output through a live ACP
  instance's `context.evaluate` before returning it, failing open to the
  command's raw output on any ACP failure (unreachable, error, timeout)
  -- see the module's own docstring for why this replaces the
  `PostToolUse` hook design originally planned (`PostToolUse.
  updatedToolOutput`/`updatedMCPToolOutput` does not work on either host
  for a tool that already succeeded).
- `report`: the same compression, but for text a caller supplies
  directly -- a subagent calls this itself, cooperatively, to compress
  its own large final report or a large inter-teammate message before
  returning/sending it, since neither host lets a hook intercept and
  replace that after the fact either (agent_protocols_v1
  background-compression adjustment §29).

`acp/adapters/hooks/subagent_report.py` holds the remaining Phase 4/C6
piece, a small host-neutral CLI script (`--agent claude|codex`) invoked as
a hook subprocess, failing open (silent no-op) on any ACP error: on
`SubagentStart`, it injects context telling the subagent about the
`report` tool; on `SubagentStop`, it enforces that ask with a bounded
block/retry (C6/D4) if the subagent's final report is oversized and
`report` wasn't called -- confirmed live on both Claude and Codex, with
Codex's handler self-limiting to exactly one block since (unlike Claude's
8-block hard cap) no host-side retry cap was observed there.

A prior "parent -> child oversized support context" hook
(`parent_child_context.py`, `PreToolUse` on the subagent-spawning tool)
was removed 2026-09-04: confirmed working on Claude, but Codex's
equivalent input-rewrite mutation (`PreToolUse.updatedInput` on
`spawn_agent`/`wait_agent`) was empirically tested and rejected outright
by Codex itself (`hook: PreToolUse Failed`, matching upstream
`openai/codex#18491`) -- a genuine, confirmed parity gap, not just an
unverified one, so the capability was dropped on both hosts rather than
kept as a Claude-only asymmetry.

`deploy/claude_code/install.py` and `deploy/codex/install.py` install
these additively into each host's live config -- `mcpServers.acp-bash`/
`permissions.deny` (Claude) or `codex mcp add` (Codex), plus the hook
entries into `hooks.SubagentStart`/`SubagentStop`. Every other existing
key/entry (e.g. agent-mem-struct's hooks) is left untouched; re-running
either script is a no-op once installed. Run with `--dry-run` to preview
first -- these change tool/hook behavior for every session on the host
they're run on, not just this project, so run them deliberately, not as
an unattended step.

## License

CC BY 4.0 — see [LICENSE](LICENSE).

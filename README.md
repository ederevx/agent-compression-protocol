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

## Claude Code host adapter

`acp/adapters/claude_code_bash_mcp.py` is a stdio MCP server exposing one
`bash` tool: it runs the command and routes its output through a live ACP
instance's `context.evaluate` before returning it, failing open to the
command's raw output on any ACP failure (unreachable, error, timeout) --
see the module's own docstring for why this replaces the `PostToolUse`
hook design originally planned (`PostToolUse.updatedToolOutput` does not
exist in real Claude Code).

`deploy/claude_code/install.py` installs it: it additively sets
`mcpServers.acp-bash` in `~/.claude.json` and adds `"Bash"` to
`permissions.deny` in `~/.claude/settings.json` (the deny rule is what
makes this MCP tool the only way to run a shell command; Claude Code has
no "prefer this tool" mechanism). Every other key in both files is left
untouched. Run with `--dry-run` to preview changes first. This changes
tool behavior for every Claude Code session on the host it's run on, not
just this project -- run it deliberately, not as an unattended step.

## License

CC BY 4.0 — see [LICENSE](LICENSE).

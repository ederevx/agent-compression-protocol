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

## License

CC BY 4.0 — see [LICENSE](LICENSE).

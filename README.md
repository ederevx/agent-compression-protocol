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
and starts `acp.ingress.Ingress` on it, publishing
`.acp/state/ingress.json` + `.acp/state/ingress.secret` for a client to
bootstrap against (see `interface/v1/README.md`'s Bootstrap section).
`--aalp-root` (or the `AALP_HOME` environment variable) is required --
it points at the AALP instance ACP compresses through, and ACP never
guesses a sibling directory for it. `--root`, `--host`, and `--port`
override the `ACP_HOME`-derived/default values.

## License

CC BY 4.0 — see [LICENSE](LICENSE).

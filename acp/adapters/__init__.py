"""Host adapters: out-of-process integrations between ACP and a real agent
host (Phase 4). Every module here talks to ACP only through its own
published interface v1 surface (see `acp.adapters.acp_client`), the same
discipline `acp.aalp_client` applies to ACP's own upstream calls into
AALP -- an adapter never imports `acp.coordinator` or another in-process
module directly, because it is meant to model a genuine separate process
(e.g. an MCP server subprocess a host spawns) talking to ACP over its
real socket boundary.
"""

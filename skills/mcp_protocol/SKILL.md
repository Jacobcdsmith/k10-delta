---
name: MCP Protocol
description: Model Context Protocol server implementation - tool discovery, invocation, and notifications over WebSocket/MQTT.
---

# MCP Protocol Skill

Implements MCP (Model Context Protocol) for device tool control.

## Capabilities

- JSON-RPC 2.0 over WebSocket/MQTT transport
- Tool discovery via `tools/list`
- Tool invocation via `tools/call`
- Device notifications via `notifications/*` methods
- User-only tools (hidden from AI by default)

## Flow

1. Device sends hello with `"mcp": true` in features
2. Backend sends `initialize` request
3. Backend calls `tools/list` to discover capabilities
4. Backend invokes tools via `tools/call`

See `docs/mcp-protocol.md` for full specification.
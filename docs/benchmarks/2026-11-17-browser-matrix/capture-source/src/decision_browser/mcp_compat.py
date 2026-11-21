"""The pinned MCP SDK's closed-argument compatibility boundary.

MCP 2.3.0 generates permissive argument models and pre-parses JSON-looking
strings. This is the only place that adapts its private tool registry. Keep
wire schemas and runtime validation together; never coerce a literal ID.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata
from pydantic import ConfigDict, create_model


class _StrictMetadata(FuncMetadata):
    def pre_parse_json(self, data: dict[str, Any]) -> dict[str, Any]:
        # MCP arguments already arrived as JSON. In particular, the string
        # "null" is an ID, not the JSON null/default-current-run sentinel.
        return data.copy()


def close_tool_arguments(server: MCPServer[Any]) -> None:
    """Adapt generated models without mutating the SDK's shared model config."""
    for tool in server._tool_manager._tools.values():
        metadata = tool.fn_metadata
        closed = create_model(
            metadata.arg_model.__name__ + "Closed",
            __base__=metadata.arg_model,
            __config__=ConfigDict(extra="forbid", strict=True),
        )
        tool.fn_metadata = _StrictMetadata(
            arg_model=closed,
            output_schema=metadata.output_schema,
            output_model=metadata.output_model,
            wrap_output=metadata.wrap_output,
        )
        tool.parameters = closed.model_json_schema(by_alias=True)

"""Tool registry.

Tools are plain async functions decorated with ``@tool``.  They are no longer
served over MCP/HTTP from inside Webots: the registry only

* records each tool (name, docstring, JSON input schema), and
* executes a tool by name when the Rust host asks for it over IPC.

No third-party package is needed (the schema is derived from the type hints).
"""
import inspect
import json
import re
import types
import typing
from typing import Any, Union, get_args, get_origin

TOOL_NAMES = []   # exported tool names (kept for get_workflow_guide)
TOOLS = {}        # name -> ToolSpec

# Tools whose job moved to the Rust host (it owns the simulation clock).
# They stay importable but are NOT exported over IPC.
HOST_OWNED = {"set_simulation_speed"}

_CATEGORY = re.compile(r"^\s*\[([^\]]+)\]")

# Toolset name = defining module under webots_mcp.tools, except where the file
# name is not a good public name. The host uses it to hide/show groups of tools.
_TOOLSET_ALIASES = {"physics_tools": "physics", "snapshot_tools": "snapshots"}


def _toolset_of(fn) -> str:
    module = getattr(fn, "__module__", "") or ""
    leaf = module.rsplit(".", 1)[-1] if "." in module else ""
    return _TOOLSET_ALIASES.get(leaf, leaf)


class ToolError(Exception):
    """Raised for protocol-level failures (unknown tool, bad arguments)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ToolSpec:
    def __init__(self, fn):
        self.name = fn.__name__
        self.fn = fn
        self.signature = inspect.signature(fn)
        self.description = inspect.getdoc(fn) or ""
        match = _CATEGORY.match(self.description)
        self.category = match.group(1) if match else ""
        self.toolset = _toolset_of(fn)
        self.input_schema = build_input_schema(fn)

    def manifest(self):
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "toolset": self.toolset,
            "inputSchema": self.input_schema,
        }


# ------------------------------------------------------------
# JSON schema from type hints
# ------------------------------------------------------------

def _schema_for(tp):
    if tp is inspect.Parameter.empty or tp is Any:
        return {}

    origin = get_origin(tp)

    if origin is Union or origin is types.UnionType:
        args = get_args(tp)
        schemas = [_schema_for(a) for a in args if a is not type(None)]
        if len(schemas) != len(args):
            schemas.append({"type": "null"})
        return schemas[0] if len(schemas) == 1 else {"anyOf": schemas}

    if origin in (list, tuple, set, frozenset):
        args = get_args(tp)
        schema = {"type": "array"}
        if args and args[0] is not Ellipsis:
            item = _schema_for(args[0])
            if item:
                schema["items"] = item
        return schema

    if origin is dict:
        args = get_args(tp)
        schema = {"type": "object"}
        if len(args) == 2:
            value = _schema_for(args[1])
            if value:
                schema["additionalProperties"] = value
        return schema

    if origin is typing.Literal:
        return {"enum": list(get_args(tp))}

    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is str:
        return {"type": "string"}
    if tp is list:
        return {"type": "array"}
    if tp is dict:
        return {"type": "object"}

    return {}


def build_input_schema(fn):
    hints = typing.get_type_hints(fn)
    properties, required = {}, []

    for name, param in inspect.signature(fn).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue

        schema = dict(_schema_for(hints.get(name, inspect.Parameter.empty)))

        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            try:
                json.dumps(param.default)
                schema["default"] = param.default
            except (TypeError, ValueError):
                pass

        properties[name] = schema

    result = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


# ------------------------------------------------------------
# Registration / dispatch
# ------------------------------------------------------------

def tool(fn):
    """Register ``fn`` as a tool (same decorator name as before)."""
    spec = ToolSpec(fn)
    TOOLS[spec.name] = spec
    if spec.name not in HOST_OWNED:
        TOOL_NAMES.append(spec.name)
    return fn


def export_manifest():
    """Tool descriptions sent to the host in the IPC ``hello``."""
    return [s.manifest() for s in TOOLS.values() if s.name not in HOST_OWNED]


async def call_tool(name, arguments):
    """Run a tool; returns its JSON-string result."""
    spec = TOOLS.get(name)

    if spec is None or name in HOST_OWNED:
        raise ToolError(-32601, f"Unknown tool: {name!r}")

    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError(-32602, "arguments must be a JSON object")

    try:
        bound = spec.signature.bind(**arguments)
    except TypeError as exc:
        raise ToolError(-32602, f"Invalid arguments for {name}: {exc}") from exc

    result = await spec.fn(*bound.args, **bound.kwargs)

    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)

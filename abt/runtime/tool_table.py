"""ToolTable — wraps source definitions into callable tool functions."""

import json as _json
from typing import Any

from ..models.source import SourceDefinition, SourceTable, ToolType
from .db import DatabaseManager


class ToolTable:
    def __init__(self, sources: dict[str, SourceDefinition], db: DatabaseManager):
        self.sources = sources
        self.db = db
        self._tools: dict[str, callable] = {}

    def build_all(self) -> dict[str, callable]:
        for source_name, source_def in self.sources.items():
            for table in source_def.tables:
                tool_name = f"{source_name}.{table.name}"
                self._tools[tool_name] = self._build_tool(source_def, table)
        return self._tools

    def _build_tool(self, source: SourceDefinition, table: SourceTable) -> callable:
        if source.type == ToolType.REST_API:
            return self._build_rest_tool(source, table)
        elif source.type == ToolType.MCP_SERVER:
            return self._build_mcp_tool(source, table)
        elif source.type == ToolType.PYTHON_FUNCTION:
            return self._build_python_tool(source, table)
        else:
            return self._build_stub_tool(source, table)

    def _build_rest_tool(self, source: SourceDefinition, table: SourceTable):
        import urllib.request
        import urllib.error

        base_url = source.config.get("base_url", "")
        auth_header = source.config.get("auth_header", "")

        def rest_tool(**kwargs) -> dict:
            cache_key = f"rest:{source.name}.{table.name}:{_json.dumps(kwargs, sort_keys=True)}"
            cached = self.db.get_cached_tool_result(cache_key)
            if cached:
                return cached

            url = f"{base_url}{table.endpoint}"
            if kwargs:
                params = "&".join(f"{k}={v}" for k, v in kwargs.items())
                url = f"{url}?{params}"

            req = urllib.request.Request(url, method=table.method)
            if auth_header:
                req.add_header("Authorization", auth_header)

            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = _json.loads(resp.read().decode())

                result = self._extract_path(data, table.result_path)

                self.db.cache_tool_result(
                    run_id="", cache_key=cache_key,
                    source_name=source.name, table_name=table.name,
                    input_params=kwargs, output_data=result,
                )
                return result
            except urllib.error.URLError as e:
                raise RuntimeError(f"Tool {source.name}.{table.name} failed: {e}")

        rest_tool.__name__ = f"{source.name}_{table.name}"
        return rest_tool

    def _build_mcp_tool(self, source: SourceDefinition, table: SourceTable):
        def mcp_stub(**kwargs) -> dict:
            return {"status": "mcp_not_connected", "message": "MCP tool stub"}
        mcp_stub.__name__ = f"{source.name}_{table.name}"
        return mcp_stub

    def _build_python_tool(self, source: SourceDefinition, table: SourceTable):
        if not table.module or not table.function:
            return self._build_stub_tool(source, table)
        import importlib
        mod = importlib.import_module(table.module)
        fn = getattr(mod, table.function)

        def python_tool(**kwargs) -> dict:
            cache_key = f"py:{source.name}.{table.name}:{_json.dumps(kwargs, sort_keys=True)}"
            cached = self.db.get_cached_tool_result(cache_key)
            if cached:
                return cached
            result = fn(**kwargs)
            if not isinstance(result, dict):
                result = {"result": result}
            self.db.cache_tool_result(
                run_id="", cache_key=cache_key,
                source_name=source.name, table_name=table.name,
                input_params=kwargs, output_data=result,
            )
            return result

        python_tool.__name__ = f"{source.name}_{table.name}"
        return python_tool

    def _build_stub_tool(self, source: SourceDefinition, table: SourceTable):
        def stub(**kwargs) -> dict:
            return {"status": "ok", "data": f"Stub for {source.name}.{table.name}"}
        stub.__name__ = f"{source.name}_{table.name}"
        return stub

    @staticmethod
    def _extract_path(data: dict | list, path: str) -> Any:
        if not path:
            return data
        for part in path.split("."):
            if isinstance(data, dict):
                data = data.get(part, data)
            elif isinstance(data, list) and part.isdigit():
                data = data[int(part)]
        return data

    def get_tools_for_node(self, tool_refs: list[str]) -> list[callable]:
        return [self._tools[ref] for ref in tool_refs if ref in self._tools]

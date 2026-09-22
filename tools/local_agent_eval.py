#!/usr/bin/env python3
"""Run a local agent and send its result to PandaProbe without an SDK."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import importlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


TERMINAL_EVAL_STATES = {"COMPLETED", "FAILED"}
EVENT_KINDS = {
    "chain": "CHAIN",
    "chat_model": "LLM",
    "llm": "LLM",
    "retriever": "RETRIEVER",
    "tool": "TOOL",
}


class BridgeError(RuntimeError):
    """A user-facing bridge error."""


def utc_now() -> str:
    """Return a UTC timestamp accepted by the PandaProbe API."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_env_file(path: str) -> None:
    """Load missing environment variables from a simple KEY=VALUE file."""
    env_path = Path(path)
    if not env_path.is_file():
        raise BridgeError(f"找不到环境变量文件：{env_path}")
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def load_json_object(value: str | None, option_name: str) -> dict[str, Any]:
    """Parse an optional command-line JSON object."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"{option_name} 不是有效的 JSON：{exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise BridgeError(f"{option_name} 必须是 JSON 对象，例如：{{\"model\": \"openai/gpt-4o-mini\"}}")
    return parsed


def resolve_project_path(project_dir: Path, value: str) -> Path:
    """Resolve a path from langgraph.json relative to the Agent project."""
    path = Path(value)
    return path if path.is_absolute() else (project_dir / path).resolve()


def discover_langgraph_project(agent_dir: str, graph_id: str | None) -> tuple[Path, str, Path | None, list[Path]]:
    """Read a LangGraph project and return its graph entrypoint and import paths."""
    project_dir = Path(agent_dir).expanduser().resolve()
    if not project_dir.is_dir():
        raise BridgeError(f"找不到 Agent 目录：{project_dir}")

    config_path = project_dir / "langgraph.json"
    if not config_path.is_file():
        raise BridgeError(f"Agent 目录中找不到 langgraph.json：{config_path}")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise BridgeError(f"langgraph.json 格式无效：{exc.msg}") from exc

    graphs = config.get("graphs")
    if not isinstance(graphs, dict) or not graphs:
        raise BridgeError("langgraph.json 中没有可用的 graphs 配置")
    selected_id = graph_id or next(iter(graphs))
    if selected_id not in graphs:
        available = "、".join(str(item) for item in graphs)
        raise BridgeError(f"找不到 Graph {selected_id!r}；可用项：{available}")
    entrypoint = graphs[selected_id]
    if not isinstance(entrypoint, str) or ":" not in entrypoint:
        raise BridgeError(f"Graph {selected_id!r} 的入口无效：{entrypoint!r}")

    import_paths = [project_dir]
    source_dir = project_dir / "src"
    if source_dir.is_dir():
        import_paths.insert(0, source_dir)
    for dependency in config.get("dependencies") or []:
        if not isinstance(dependency, str):
            continue
        dependency_path = resolve_project_path(project_dir, dependency)
        if dependency_path.is_dir():
            dependency_src = dependency_path / "src"
            for candidate in (dependency_src, dependency_path):
                if candidate.is_dir() and candidate not in import_paths:
                    import_paths.append(candidate)

    env_value = config.get("env")
    env_path = resolve_project_path(project_dir, env_value) if isinstance(env_value, str) else None
    return project_dir, entrypoint, env_path, import_paths


@contextmanager
def agent_import_environment(project_dir: Path | None, import_paths: list[Path] | None = None) -> Iterator[None]:
    """Temporarily apply a project's working directory and Python import paths."""
    previous_cwd = Path.cwd()
    inserted: list[str] = []
    try:
        if project_dir is not None:
            os.chdir(project_dir)
        for path in reversed(import_paths or []):
            text_path = str(path)
            if text_path not in sys.path:
                sys.path.insert(0, text_path)
                inserted.append(text_path)
        yield
    finally:
        os.chdir(previous_cwd)
        for text_path in inserted:
            if text_path in sys.path:
                sys.path.remove(text_path)


def request_json(
    method: str,
    url: str,
    *,
    payload: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> Any:
    """Make a JSON HTTP request using only the Python standard library."""
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body
        try:
            parsed = json.loads(body)
            detail = parsed.get("detail") or parsed.get("message") or body
        except json.JSONDecodeError:
            pass
        raise BridgeError(f"PandaProbe 请求失败（HTTP {exc.code}）：{detail}") from exc
    except URLError as exc:
        raise BridgeError(f"无法连接 {url}：{exc.reason}") from exc


def get_nested_value(value: Any, path: str | None) -> Any:
    """Read a dotted field such as data.answer from a JSON response."""
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise BridgeError(f"Agent 返回内容中找不到字段：{path}")
    return current


def to_jsonable(value: Any) -> Any:
    """Convert LangChain message and model objects into JSON-safe values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return to_jsonable(value.model_dump(mode="json"))
        except TypeError:
            return to_jsonable(value.model_dump())
    return str(value)


def final_answer(value: Any) -> Any:
    """Extract the final assistant content from a LangGraph result when possible."""
    if isinstance(value, dict) and value.get("messages"):
        message = value["messages"][-1]
        if isinstance(message, dict):
            return message.get("content", message)
        return getattr(message, "content", message)
    return value


def parse_event_name(name: str) -> tuple[str | None, str | None]:
    """Split on_tool_start into (tool, start)."""
    if not name.startswith("on_"):
        return None, None
    body = name[3:]
    for action in ("start", "end", "error"):
        suffix = f"_{action}"
        if body.endswith(suffix):
            return body[: -len(suffix)], action
    return None, None


def import_attribute(factory_path: str, project_dir: Path | None = None) -> Any:
    """Import an object from either module:attribute or path.py:attribute."""
    if ":" not in factory_path:
        raise BridgeError("LangGraph 入口必须使用 模块:属性 或 文件.py:属性 格式")
    module_ref, attribute_name = factory_path.rsplit(":", 1)
    try:
        file_candidate = Path(module_ref)
        if not file_candidate.is_absolute() and project_dir is not None:
            file_candidate = project_dir / file_candidate
        if file_candidate.suffix == ".py" and file_candidate.is_file():
            resolved_file = file_candidate.resolve()
            module = None
            for import_root in sys.path:
                try:
                    relative_file = resolved_file.relative_to(Path(import_root or Path.cwd()).resolve())
                except (OSError, ValueError):
                    continue
                module_parts = list(relative_file.with_suffix("").parts)
                if module_parts[-1:] == ["__init__"]:
                    module_parts.pop()
                if module_parts and all(part.isidentifier() for part in module_parts):
                    module = importlib.import_module(".".join(module_parts))
                    break
            if module is None:
                module_name = f"_local_agent_{abs(hash(str(resolved_file)))}"
                spec = importlib.util.spec_from_file_location(module_name, resolved_file)
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot load {resolved_file}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
        else:
            local_module = Path.cwd().joinpath(*module_ref.split(".")).with_suffix(".py")
            if local_module.is_file():
                module_name = f"_local_agent_{module_ref.replace('.', '_')}"
                spec = importlib.util.spec_from_file_location(module_name, local_module)
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot load {local_module}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            else:
                module = importlib.import_module(module_ref)
        return getattr(module, attribute_name)
    except (ImportError, AttributeError, OSError) as exc:
        raise BridgeError(f"无法导入 LangGraph Agent：{factory_path}（{exc}）") from exc


def create_runtime_context(runnable: Any, values: dict[str, Any]) -> Any | None:
    """Instantiate a compiled graph's context schema when it declares one."""
    context_schema = getattr(runnable, "context_schema", None)
    if context_schema is None:
        context_schema = getattr(getattr(runnable, "builder", None), "context_schema", None)
    if context_schema is None:
        if values:
            raise BridgeError("该 Graph 没有公开 context_schema，无法使用 --context-json")
        return None
    try:
        return context_schema(**values)
    except TypeError as exc:
        raise BridgeError(f"无法创建 LangGraph Context：{exc}") from exc


async def collect_langgraph_events(
    factory_path: str,
    prompt: str,
    *,
    project_dir: Path | None = None,
    import_paths: list[Path] | None = None,
    context_values: dict[str, Any] | None = None,
) -> tuple[Any, list[dict[str, Any]], int]:
    """Import a Runnable and collect its standard LangChain v2 events."""
    with agent_import_environment(project_dir, import_paths):
        attribute = import_attribute(factory_path, project_dir)
        runnable = attribute if hasattr(attribute, "astream_events") else attribute()
        if not hasattr(runnable, "astream_events"):
            raise BridgeError(f"{factory_path} 返回的对象不支持 astream_events()")
        runtime_context = create_runtime_context(runnable, context_values or {})

        records: dict[str, dict[str, Any]] = {}
        record_order: list[str] = []
        final_result: Any = None
        event_count = 0
        event_kwargs: dict[str, Any] = {"version": "v2"}
        if runtime_context is not None:
            event_kwargs["context"] = runtime_context

        async for event in runnable.astream_events(
            {"messages": [{"role": "user", "content": prompt}]},
            **event_kwargs,
        ):
            event_count += 1
            component, action = parse_event_name(str(event.get("event", "")))
            if component not in EVENT_KINDS or action is None:
                continue

            run_id = str(event.get("run_id", ""))
            data = event.get("data") or {}
            if action == "start" and run_id not in records and len(records) < 450:
                records[run_id] = {
                    "span_id": str(uuid4()),
                    "run_id": run_id,
                    "parent_run_ids": [str(item) for item in event.get("parent_ids", [])],
                    "name": str(event.get("name") or component),
                    "kind": EVENT_KINDS[component],
                    "status": "UNSET",
                    "input": to_jsonable(data.get("input")),
                    "output": None,
                    "model": (event.get("metadata") or {}).get("ls_model_name")
                    if component in {"llm", "chat_model"}
                    else None,
                    "metadata": {
                        "langchain_event": event.get("event"),
                        "tags": to_jsonable(event.get("tags") or []),
                        "langchain_metadata": to_jsonable(event.get("metadata") or {}),
                    },
                    "started_at": utc_now(),
                    "ended_at": None,
                    "error": None,
                }
                record_order.append(run_id)
            elif action in {"end", "error"} and run_id in records:
                record = records[run_id]
                record["ended_at"] = utc_now()
                if action == "end":
                    record["status"] = "OK"
                    record["output"] = to_jsonable(data.get("output"))
                    if not event.get("parent_ids") and component == "chain":
                        final_result = data.get("output")
                else:
                    record["status"] = "ERROR"
                    record["error"] = str(data.get("error") or "LangChain event failed")

        spans: list[dict[str, Any]] = []
        for run_id in record_order:
            record = records[run_id]
            parent_span_id = next(
                (
                    records[parent_id]["span_id"]
                    for parent_id in reversed(record.pop("parent_run_ids"))
                    if parent_id in records
                ),
                None,
            )
            record.pop("run_id", None)
            record["parent_span_id"] = parent_span_id
            if record["ended_at"] is None:
                record["ended_at"] = utc_now()
            spans.append(record)

        return final_answer(final_result), spans, event_count


def run_langgraph_agent(
    factory_path: str,
    prompt: str,
    timeout: float,
    *,
    project_dir: Path | None = None,
    import_paths: list[Path] | None = None,
    context_values: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any], bool, list[dict[str, Any]]]:
    """Run a LangGraph Runnable in-process and preserve its internal events."""
    started = time.perf_counter()
    try:
        output, spans, event_count = asyncio.run(
            asyncio.wait_for(
                collect_langgraph_events(
                    factory_path,
                    prompt,
                    project_dir=project_dir,
                    import_paths=import_paths,
                    context_values=context_values,
                ),
                timeout=timeout,
            )
        )
        succeeded = True
        error = None
    except TimeoutError:
        output, spans, event_count = f"Agent 运行超过 {timeout:g} 秒，已停止", [], 0
        succeeded = False
        error = output
    except BridgeError:
        raise
    except Exception as exc:
        output, spans, event_count = str(exc), [], 0
        succeeded = False
        error = output

    metadata = {
        "adapter": "langgraph-events",
        "factory": factory_path,
        "agent_dir": str(project_dir) if project_dir else None,
        "event_count": event_count,
        "captured_spans": len(spans),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    if error:
        metadata["error"] = str(error)[-4000:]
    return to_jsonable(output), metadata, succeeded, spans


def run_command_agent(command: str, prompt: str, timeout: float) -> tuple[Any, dict[str, Any], bool]:
    """Run a local command, passing the prompt through stdin or {prompt}."""
    args = shlex.split(command, posix=os.name != "nt")
    if not args:
        raise BridgeError("--command 不能为空")

    uses_placeholder = any("{prompt}" in part for part in args)
    if uses_placeholder:
        args = [part.replace("{prompt}", prompt) for part in args]

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            args,
            input=None if uses_placeholder else prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BridgeError(f"找不到 Agent 命令：{args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"Agent 运行超过 {timeout:g} 秒，已停止") from exc

    output_text = completed.stdout.strip()
    if not output_text and completed.returncode != 0:
        output_text = completed.stderr.strip()

    try:
        output: Any = json.loads(output_text)
    except json.JSONDecodeError:
        output = output_text

    metadata = {
        "adapter": "local-command",
        "command": args[0],
        "exit_code": completed.returncode,
        "stderr": completed.stderr.strip()[-4000:],
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    return output, metadata, completed.returncode == 0


def run_http_agent(
    url: str,
    prompt: str,
    prompt_field: str,
    response_field: str | None,
    timeout: float,
) -> tuple[Any, dict[str, Any], bool]:
    """Call a local HTTP agent with a small JSON request."""
    started = time.perf_counter()
    try:
        response = request_json("POST", url, payload={prompt_field: prompt}, timeout=timeout)
    except BridgeError as exc:
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        return str(exc), {"adapter": "local-http", "url": url, "duration_ms": elapsed}, False

    output = get_nested_value(response, response_field)
    metadata = {
        "adapter": "local-http",
        "url": url,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    return output, metadata, True


def discover_local_project(api_base: str, project_name: str, timeout: float) -> str:
    """Find a project ID through the management API in local development mode."""
    organizations = request_json("GET", f"{api_base}/organizations", timeout=timeout)
    if not organizations:
        raise BridgeError("本地 PandaProbe 中还没有组织，请先打开网页完成初始化")

    matches: list[dict[str, Any]] = []
    for organization in organizations:
        org_id = organization.get("id") or organization.get("organization", {}).get("id")
        if not org_id:
            continue
        projects = request_json("GET", f"{api_base}/organizations/{org_id}/projects", timeout=timeout)
        matches.extend(project for project in projects if project.get("name") == project_name)

    if not matches:
        raise BridgeError(f"找不到本地项目 {project_name!r}，请用 --project-name 指定页面左上角的项目名")
    return str(matches[0]["id"])


def panda_headers(args: argparse.Namespace) -> dict[str, str]:
    """Build data-plane headers for API-key or local-development auth."""
    api_key = args.api_key or os.environ.get("PANDAPROBE_API_KEY")
    if api_key:
        return {"X-API-Key": api_key, "X-Project-Name": args.project_name}
    project_id = args.project_id or discover_local_project(args.api_base, args.project_name, args.timeout)
    return {"X-Project-ID": project_id}


def wait_for_trace(api_base: str, trace_id: str, headers: dict[str, str], timeout: float) -> None:
    """Wait until asynchronous trace ingestion has completed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request_json("GET", f"{api_base}/traces/{trace_id}", headers=headers, timeout=min(10, timeout))
            return
        except BridgeError as exc:
            if "HTTP 404" not in str(exc):
                raise
        time.sleep(0.5)
    raise BridgeError("Trace 已提交，但等待写入数据库超时；稍后可在 Traces 页面查看")


def evaluate_trace(
    api_base: str,
    trace_id: str,
    metrics: list[str],
    model: str | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Create an eval run and wait for its scores."""
    payload: dict[str, Any] = {
        "trace_ids": [trace_id],
        "metrics": metrics,
        "name": f"Local agent: {trace_id[:8]}",
    }
    if model:
        payload["model"] = model
    run = request_json(
        "POST",
        f"{api_base}/evaluations/trace-runs/batch",
        payload=payload,
        headers=headers,
        timeout=timeout,
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = request_json(
            "GET", f"{api_base}/evaluations/trace-runs/{run['id']}", headers=headers, timeout=min(10, timeout)
        )
        if run["status"] in TERMINAL_EVAL_STATES:
            scores = request_json(
                "GET",
                f"{api_base}/evaluations/trace-runs/{run['id']}/scores",
                headers=headers,
                timeout=min(10, timeout),
            )
            return run, scores
        time.sleep(1)
    raise BridgeError(f"评测仍在后台运行。运行 ID：{run['id']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行本地 Agent，并通过 REST API 记录到 PandaProbe（无需 SDK）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    agent = parser.add_mutually_exclusive_group(required=True)
    agent.add_argument("--command", help='本地命令，例如：python my_agent.py（prompt 默认从 stdin 传入）')
    agent.add_argument("--agent-url", help="本地 Agent 的 HTTP POST 地址")
    agent.add_argument("--langgraph", help="可导入的 LangGraph 工厂，格式为 模块:函数")
    agent.add_argument("--agent-dir", help="标准 LangGraph 项目目录；自动读取 langgraph.json")
    parser.add_argument("--graph-id", help="langgraph.json 中的 Graph 名称；不填时使用第一项")
    parser.add_argument(
        "--context-json",
        help='传给 LangGraph Context 的 JSON 对象，例如：{"model":"openai/gpt-4o-mini"}',
    )
    parser.add_argument("--prompt", required=True, help="交给 Agent 的问题或任务")
    parser.add_argument("--prompt-field", default="prompt", help="HTTP Agent 接收 prompt 的 JSON 字段")
    parser.add_argument("--response-field", help="HTTP Agent 响应中作为结果的字段，支持 data.answer 形式")
    parser.add_argument("--name", default="Local Agent", help="PandaProbe 中显示的 Trace 名称")
    parser.add_argument("--metric", action="append", default=[], help="评测指标；可重复填写。不填时只记录 Trace")
    parser.add_argument("--judge-model", help="覆盖评测模型；不填则使用后端 EVAL_LLM_MODEL")
    parser.add_argument("--api-base", default="http://localhost:8000", help="PandaProbe API 地址")
    parser.add_argument("--project-name", default="onboarding", help="PandaProbe 项目名")
    parser.add_argument("--project-id", help="本地开发项目 ID；通常可自动发现")
    parser.add_argument("--api-key", help="PandaProbe API key；也可用 PANDAPROBE_API_KEY 环境变量")
    parser.add_argument("--env-file", help="运行 Agent 前读取的 KEY=VALUE 文件，例如 backend/.env.development")
    parser.add_argument("--session-id", help="可选的会话 ID，用于把多次调用归为一个 Session")
    parser.add_argument("--tag", action="append", default=["local-agent"], help="Trace 标签；可重复填写")
    parser.add_argument("--timeout", type=float, default=120, help="Agent、写入和评测的最长等待秒数")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.api_base = args.api_base.rstrip("/")

    try:
        graph_project_dir: Path | None = None
        graph_import_paths: list[Path] = []
        graph_entrypoint = args.langgraph
        automatic_env_path: Path | None = None
        if args.agent_dir:
            graph_project_dir, graph_entrypoint, automatic_env_path, graph_import_paths = discover_langgraph_project(
                args.agent_dir, args.graph_id
            )
        env_path = Path(args.env_file).expanduser().resolve() if args.env_file else automatic_env_path
        if env_path is not None:
            load_env_file(str(env_path))
        context_values = load_json_object(args.context_json, "--context-json")
        started_at = utc_now()
        detailed_spans: list[dict[str, Any]] = []
        if args.command:
            output, metadata, succeeded = run_command_agent(args.command, args.prompt, args.timeout)
        elif args.agent_url:
            output, metadata, succeeded = run_http_agent(
                args.agent_url, args.prompt, args.prompt_field, args.response_field, args.timeout
            )
        else:
            if not graph_entrypoint:
                raise BridgeError("没有可用的 LangGraph 入口")
            output, metadata, succeeded, detailed_spans = run_langgraph_agent(
                graph_entrypoint,
                args.prompt,
                args.timeout,
                project_dir=graph_project_dir,
                import_paths=graph_import_paths,
                context_values=context_values,
            )
        ended_at = utc_now()
        headers = panda_headers(args)
        trace_id = str(uuid4())
        error = None if succeeded else str(output)
        agent_span_id = str(uuid4())
        for span in detailed_spans:
            if span.get("parent_span_id") is None:
                span["parent_span_id"] = agent_span_id
        trace_payload = {
            "trace_id": trace_id,
            "name": args.name,
            "status": "COMPLETED" if succeeded else "ERROR",
            "input": args.prompt,
            "output": output,
            "metadata": metadata,
            "started_at": started_at,
            "ended_at": ended_at,
            "session_id": args.session_id,
            "tags": list(dict.fromkeys(args.tag)),
            "environment": "local",
            "spans": [
                {
                    "span_id": agent_span_id,
                    "name": args.name,
                    "kind": "AGENT",
                    "status": "OK" if succeeded else "ERROR",
                    "input": args.prompt,
                    "output": output,
                    "metadata": metadata,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "error": error,
                },
                *detailed_spans,
            ],
        }
        accepted = request_json(
            "POST", f"{args.api_base}/traces", payload=trace_payload, headers=headers, timeout=args.timeout
        )
        wait_for_trace(args.api_base, trace_id, headers, args.timeout)

        result: dict[str, Any] = {
            "agent_succeeded": succeeded,
            "agent_output": output,
            "trace_id": accepted["trace_id"],
            "trace_url": f"http://localhost:3000",
        }
        if args.metric:
            run, scores = evaluate_trace(
                args.api_base, trace_id, list(dict.fromkeys(args.metric)), args.judge_model, headers, args.timeout
            )
            result["evaluation"] = {
                "run_id": run["id"],
                "status": run["status"],
                "error": run.get("error_message"),
                "scores": [
                    {
                        "name": score["name"],
                        "value": score["value"],
                        "status": score["status"],
                        "reason": score.get("reason"),
                    }
                    for score in scores
                ],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if succeeded else 2
    except BridgeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

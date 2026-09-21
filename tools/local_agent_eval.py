#!/usr/bin/env python3
"""Run a local agent and send its result to PandaProbe without an SDK."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


TERMINAL_EVAL_STATES = {"COMPLETED", "FAILED"}


class BridgeError(RuntimeError):
    """A user-facing bridge error."""


def utc_now() -> str:
    """Return a UTC timestamp accepted by the PandaProbe API."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
    parser.add_argument("--session-id", help="可选的会话 ID，用于把多次调用归为一个 Session")
    parser.add_argument("--tag", action="append", default=["local-agent"], help="Trace 标签；可重复填写")
    parser.add_argument("--timeout", type=float, default=120, help="Agent、写入和评测的最长等待秒数")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.api_base = args.api_base.rstrip("/")
    started_at = utc_now()

    try:
        if args.command:
            output, metadata, succeeded = run_command_agent(args.command, args.prompt, args.timeout)
        else:
            output, metadata, succeeded = run_http_agent(
                args.agent_url, args.prompt, args.prompt_field, args.response_field, args.timeout
            )
        ended_at = utc_now()
        headers = panda_headers(args)
        trace_id = str(uuid4())
        error = None if succeeded else str(output)
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
                    "name": args.name,
                    "kind": "AGENT",
                    "status": "OK" if succeeded else "ERROR",
                    "input": args.prompt,
                    "output": output,
                    "metadata": metadata,
                    "started_at": started_at,
                    "ended_at": ended_at,
                    "error": error,
                }
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

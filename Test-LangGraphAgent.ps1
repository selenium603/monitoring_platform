param(
    [Parameter(Position = 0)]
    [string]$Prompt = "What's the weather in Tokyo?",

    [string]$SessionId,

    [switch]$Evaluate
)

$projectRoot = $PSScriptRoot
$bridgeArgs = @(
    "-B",
    (Join-Path $projectRoot "tools\local_agent_eval.py"),
    "--langgraph", "test:build_agent",
    "--env-file", (Join-Path $projectRoot "backend\.env.development"),
    "--prompt", $Prompt,
    "--name", "LangGraph Event Test"
)

if ($SessionId) {
    $bridgeArgs += @("--session-id", $SessionId)
}

if ($Evaluate) {
    $bridgeArgs += @(
        "--metric", "task_completion",
        "--metric", "tool_correctness",
        "--metric", "argument_correctness"
    )
}

Push-Location $projectRoot
try {
    & python @bridgeArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

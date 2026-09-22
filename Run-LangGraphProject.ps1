param(
    [Parameter(Mandatory)]
    [string]$AgentDir,

    [Parameter(Mandatory)]
    [string]$Prompt,

    [string]$Name = "LangGraph Agent",
    [string]$SessionId,
    [string]$GraphId,
    [string]$ContextJson,
    [string]$ProjectName = "onboarding",
    [switch]$Evaluate
)

$projectRoot = $PSScriptRoot
$resolvedAgentDir = (Resolve-Path -LiteralPath $AgentDir).Path
$pythonCandidates = @(
    (Join-Path $resolvedAgentDir ".venv\Scripts\python.exe"),
    (Join-Path $resolvedAgentDir "venv\Scripts\python.exe")
)
$agentPython = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $agentPython) {
    $agentPython = (Get-Command python -ErrorAction Stop).Source
}

$bridgeArgs = @(
    "-B",
    (Join-Path $projectRoot "tools\local_agent_eval.py"),
    "--agent-dir", $resolvedAgentDir,
    "--prompt", $Prompt,
    "--name", $Name,
    "--project-name", $ProjectName
)
if ($SessionId) {
    $bridgeArgs += @("--session-id", $SessionId)
}
if ($GraphId) {
    $bridgeArgs += @("--graph-id", $GraphId)
}
if ($ContextJson) {
    $bridgeArgs += @("--context-json", $ContextJson)
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
    & $agentPython @bridgeArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

param(
    [Parameter(Mandatory)]
    [string]$AgentDir,

    [Parameter(Mandatory)]
    [string]$Prompt,

    [string]$Name = "LangGraph Agent",
    [string]$SessionId,
    [string]$GraphId,
    [string]$Entrypoint,
    [ValidateSet("auto", "messages", "input", "query", "question", "prompt", "string")]
    [string]$InputMode = "auto",
    [string]$InputJson,
    [string]$OutputPath,
    [string]$ConfigJson,
    [string]$FactoryJson,
    [string]$ThreadId,
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
    "--input-mode", $InputMode,
    "--project-name", $ProjectName
)
if ($SessionId) {
    $bridgeArgs += @("--session-id", $SessionId)
}
if ($GraphId) {
    $bridgeArgs += @("--graph-id", $GraphId)
}
if ($Entrypoint) {
    $bridgeArgs += @("--entrypoint", $Entrypoint)
}
if ($InputJson) {
    $bridgeArgs += @("--input-json", $InputJson)
}
if ($OutputPath) {
    $bridgeArgs += @("--output-path", $OutputPath)
}
if ($ConfigJson) {
    $bridgeArgs += @("--config-json", $ConfigJson)
}
if ($FactoryJson) {
    $bridgeArgs += @("--factory-json", $FactoryJson)
}
if ($ThreadId) {
    $bridgeArgs += @("--thread-id", $ThreadId)
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

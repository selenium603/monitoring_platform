$ErrorActionPreference = 'Stop'

$dockerExe = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
if (-not (Test-Path -LiteralPath $dockerExe)) {
    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $dockerCommand) { throw 'Docker Desktop was not found.' }
    $dockerExe = $dockerCommand.Source
}

& $dockerExe info *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop is not ready. Open it and wait for the engine to start.' }

& $dockerExe compose --project-directory $PSScriptRoot up -d --build --wait --wait-timeout 300
if ($LASTEXITCODE -ne 0) { throw 'PandaProbe startup failed. Open Docker Desktop and inspect the project logs.' }

Write-Host 'PandaProbe: http://localhost:3000'
Write-Host 'API documentation: http://localhost:8000/docs'

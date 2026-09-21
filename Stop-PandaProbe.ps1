$ErrorActionPreference = 'Stop'

$dockerExe = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
if (-not (Test-Path -LiteralPath $dockerExe)) {
    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $dockerCommand) { throw 'Docker Desktop was not found.' }
    $dockerExe = $dockerCommand.Source
}

& $dockerExe compose --project-directory $PSScriptRoot stop
if ($LASTEXITCODE -ne 0) { throw 'PandaProbe could not be stopped.' }

Write-Host 'PandaProbe stopped. Database contents are preserved.'

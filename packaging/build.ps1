$ErrorActionPreference = 'Stop'
try {
    $projectRoot = Split-Path $PSScriptRoot -Parent
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $python = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
    }
    if (-not $python) {
        $python = (Get-Command python.exe -ErrorAction Stop).Source
    }
    $outputRoot = Join-Path $projectRoot ('dist\LocalWorkflow-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
    New-Item -ItemType Directory -Path $outputRoot | Out-Null
    Write-Host 'Bundling Python, application files, and dependencies...'
    & $python (Join-Path $PSScriptRoot 'build_portable.py') $outputRoot
    if ($LASTEXITCODE -ne 0) { throw 'Python packaging or runtime validation failed.' }
    Add-Type -Path (Join-Path $PSScriptRoot 'Launcher.cs') -OutputAssembly (Join-Path $outputRoot 'LocalWorkflow.exe') -OutputType ConsoleApplication
    & $python (Join-Path $PSScriptRoot 'build_portable.py') $outputRoot --zip
    if ($LASTEXITCODE -ne 0) { throw 'ZIP creation failed.' }
    Write-Host "`nBuild complete: $outputRoot.zip"
    Write-Host 'Share the ZIP. Users must extract the entire folder before launching LocalWorkflow.exe.'
} catch {
    Write-Host "Build failed: $_" -ForegroundColor Red
    exit 1
}

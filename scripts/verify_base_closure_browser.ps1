$BrowserTest = 'verify-base-closure-browser.mjs'
$ErrorActionPreference = 'Stop'
$taskDetailRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskDetailFront = (Resolve-Path (Join-Path $taskDetailRoot '../frontend')).Path
$taskDetailOutput = Join-Path $taskDetailRoot 'docs/reviews/evidencias_bloque4_20261029/navegador'
$taskDetailTemp = Join-Path ([IO.Path]::GetTempPath()) ('gm-detail-' + [Guid]::NewGuid().ToString('N'))
$taskDetailContainer = $null
$taskDetailServer = $null
$taskDetailVite = $null
$taskDetailSaved = @{}
foreach ($taskDetailKey in @('MYSQL_ROOT_PASSWORD','MYSQL_PWD','GM_TEST_MYSQL_PASSWORD','GM_TEST_MYSQL_PORT','DJANGO_SETTINGS_MODULE','FRONTEND_ORIGIN','VITE_API_BASE_URL','API_PROXY_TARGET','GM_TASK_DETAIL_BROWSER','GM_TASK_LIST_BROWSER','GM_QA_SECRETS_PATH','GM_BROWSER_REPORT_DIR')) {
    $taskDetailSaved[$taskDetailKey] = [Environment]::GetEnvironmentVariable($taskDetailKey, 'Process')
}
try {
    $taskDetailPorts = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners().Port
    if (8819 -in $taskDetailPorts -or 5189 -in $taskDetailPorts) { throw 'Los puertos aislados están ocupados. No se reutilizarán servidores existentes.' }
    New-Item -ItemType Directory -Path $taskDetailTemp | Out-Null
    New-Item -ItemType Directory -Force -Path $taskDetailOutput | Out-Null
    $env:MYSQL_ROOT_PASSWORD = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
    $env:MYSQL_PWD = $env:MYSQL_ROOT_PASSWORD
    $env:GM_TEST_MYSQL_PASSWORD = $env:MYSQL_ROOT_PASSWORD
    $taskDetailContainer = (docker run --detach --label gm.base-closure-browser=true --env MYSQL_ROOT_PASSWORD --publish 127.0.0.1::3306 mysql:8.4 --skip-log-bin).Trim()
    if ($LASTEXITCODE -ne 0 -or $taskDetailContainer -notmatch '^[a-f0-9]{64}$') { throw 'No se pudo crear MySQL temporal.' }
    $taskDetailReady = $false
    for ($taskDetailAttempt = 0; $taskDetailAttempt -lt 60; $taskDetailAttempt++) {
        $ErrorActionPreference = 'Continue'
        $taskDetailProbe = docker exec --env MYSQL_PWD $taskDetailContainer mysql --protocol=TCP --host=127.0.0.1 --user=root --execute 'SELECT 1' 2>&1
        $taskDetailProbeExit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($taskDetailProbeExit -eq 0) { $taskDetailReady = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $taskDetailReady) { throw 'MySQL temporal no está listo.' }
    $taskDetailNetwork = docker inspect --format '{{json .NetworkSettings.Ports}}' $taskDetailContainer | ConvertFrom-Json
    $env:GM_TEST_MYSQL_PORT = $taskDetailNetwork.'3306/tcp'[0].HostPort
    docker exec --env MYSQL_PWD $taskDetailContainer mysql --user=root --execute 'CREATE DATABASE gm_test_task_status_templates CHARACTER SET utf8mb4'
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear la base nueva aislada.' }
    $env:DJANGO_SETTINGS_MODULE = 'config.settings_test_mysql_templates'
    $env:FRONTEND_ORIGIN = 'http://localhost:5189'
    $env:VITE_API_BASE_URL = 'http://localhost:8819'
    if ($BrowserTest -eq 'verify-auth-routing-browser.mjs') {
        $env:VITE_API_BASE_URL = ''
        $env:API_PROXY_TARGET = 'http://127.0.0.1:8819'
    }
    $env:GM_TASK_DETAIL_BROWSER = '1'
    if ($BrowserTest -eq 'verify-list-presentation-browser.mjs') { $env:GM_TASK_LIST_BROWSER = '1' }
    $env:GM_QA_SECRETS_PATH = Join-Path $taskDetailTemp 'credentials.json'
    $env:GM_BROWSER_REPORT_DIR = $taskDetailOutput
    $taskDetailServer = Start-Process -FilePath (Join-Path $taskDetailRoot '.venv/Scripts/python.exe') -ArgumentList '-m apps.projects.tests.task_detail_server' -WorkingDirectory $taskDetailRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskDetailTemp 'server.log') -RedirectStandardError (Join-Path $taskDetailTemp 'server-errors.log')
    $taskDetailVite = Start-Process -FilePath (Get-Command node.exe).Source -ArgumentList 'node_modules/vite/bin/vite.js --host localhost --port 5189 --strictPort' -WorkingDirectory $taskDetailFront -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskDetailTemp 'vite.log') -RedirectStandardError (Join-Path $taskDetailTemp 'vite-errors.log')
    for ($taskDetailAttempt = 0; $taskDetailAttempt -lt 90 -and -not (Test-Path -LiteralPath $env:GM_QA_SECRETS_PATH); $taskDetailAttempt++) {
        if ($taskDetailServer.HasExited) { Get-Content -LiteralPath (Join-Path $taskDetailTemp 'server-errors.log'); throw 'Falló la preparación sintética.' }
        Start-Sleep -Seconds 1
    }
    if (-not (Test-Path -LiteralPath $env:GM_QA_SECRETS_PATH)) { throw 'No se completó el arranque aislado.' }
    Push-Location $taskDetailFront
    try {
        & node.exe (Join-Path 'tests' $BrowserTest)
        if ($LASTEXITCODE -ne 0) { throw 'Falló la verificación en navegador.' }
    } finally { Pop-Location }
} finally {
    foreach ($taskDetailProcess in @($taskDetailServer, $taskDetailVite)) {
        if ($null -ne $taskDetailProcess -and -not $taskDetailProcess.HasExited) { Stop-Process -Id $taskDetailProcess.Id }
    }
    if ($taskDetailContainer -match '^[a-f0-9]{64}$') {
        $taskDetailLabels = docker inspect --format '{{json .Config.Labels}}' $taskDetailContainer | ConvertFrom-Json
        if ($LASTEXITCODE -eq 0 -and $taskDetailLabels.'gm.base-closure-browser' -eq 'true') { docker rm --force $taskDetailContainer | Out-Null }
    }
    if (Test-Path -LiteralPath $taskDetailTemp) {
        $taskDetailResolved = (Resolve-Path -LiteralPath $taskDetailTemp).Path
        if ($taskDetailResolved.StartsWith([IO.Path]::GetTempPath()) -and (Split-Path -Leaf $taskDetailResolved) -match '^gm-detail-[a-f0-9]{32}$') { Remove-Item -LiteralPath $taskDetailResolved -Recurse -Force }
    }
    foreach ($taskDetailKey in $taskDetailSaved.Keys) { [Environment]::SetEnvironmentVariable($taskDetailKey, $taskDetailSaved[$taskDetailKey], 'Process') }
    Write-Output 'Instancia MySQL, servidores y credenciales temporales eliminados; evidencias conservadas.'
}

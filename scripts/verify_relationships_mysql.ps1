param([switch]$Reproduce)
$ErrorActionPreference = 'Stop'
$taskStatusContainer = $null
$taskStatusRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskStatusPrevious = @{}
foreach ($taskStatusKey in @('MYSQL_ROOT_PASSWORD', 'MYSQL_PWD', 'GM_TEST_MYSQL_PASSWORD', 'GM_TEST_MYSQL_PORT', 'GM_G08_TEST', 'GM_G08_REPRODUCE', 'GM_G08_REPORT_DIR')) {
    $taskStatusPrevious[$taskStatusKey] = [Environment]::GetEnvironmentVariable($taskStatusKey, 'Process')
}
try {
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker no está disponible. No se ha creado ni conectado ninguna base.' }
    $taskStatusPassword = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
    $env:MYSQL_ROOT_PASSWORD = $taskStatusPassword
    $env:MYSQL_PWD = $taskStatusPassword
    $env:GM_TEST_MYSQL_PASSWORD = $taskStatusPassword
    $taskStatusName = 'gm-task-status-' + [Guid]::NewGuid().ToString('N')
    $taskStatusCreated = docker run --detach --name $taskStatusName --label gm.task-status-verification=true --env MYSQL_ROOT_PASSWORD --publish 127.0.0.1::3306 mysql:8.4 --skip-log-bin
    if ($LASTEXITCODE -ne 0 -or $taskStatusCreated -notmatch '^[a-f0-9]{64}$') { throw 'No se pudo crear el contenedor temporal.' }
    $taskStatusContainer = $taskStatusCreated.Trim()
    $taskStatusReady = $false
    for ($taskStatusAttempt = 0; $taskStatusAttempt -lt 60; $taskStatusAttempt++) {
        $ErrorActionPreference = 'Continue'
        $taskStatusProbe = docker exec --env MYSQL_PWD $taskStatusContainer mysql --protocol=TCP --host=127.0.0.1 --user=root --execute 'SELECT 1' 2>&1
        $taskStatusProbeExit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($taskStatusProbeExit -eq 0) { $taskStatusReady = $true; break }
        if ($taskStatusAttempt % 15 -eq 0) { Write-Output 'Esperando al MySQL temporal…' }
        Start-Sleep -Seconds 1
    }
    if (-not $taskStatusReady) { throw 'El MySQL temporal no inició a tiempo.' }
    $taskStatusPorts = docker inspect --format '{{json .NetworkSettings.Ports}}' $taskStatusContainer | ConvertFrom-Json
    $env:GM_TEST_MYSQL_PORT = $taskStatusPorts.'3306/tcp'[0].HostPort
    if ($LASTEXITCODE -ne 0 -or $env:GM_TEST_MYSQL_PORT -notmatch '^\d+$') { throw 'Puerto temporal no disponible.' }
    docker exec --env MYSQL_PWD $taskStatusContainer mysql --user=root --execute 'CREATE DATABASE gm_test_relationships CHARACTER SET utf8mb4'
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear la base nueva dedicada.' }
    $env:GM_G08_TEST = '1'
    $env:GM_G08_REPRODUCE = if ($Reproduce) { '1' } else { '0' }
    $phase = if ($Reproduce) { 'reproduccion' } else { 'correccion' }
    $env:GM_G08_REPORT_DIR = Join-Path $taskStatusRoot ('docs/reviews/evidencias_bloque2_20261028/' + $phase)
    Push-Location $taskStatusRoot
    try {
        & ./.venv/Scripts/python.exe -X utf8 -B manage.py test apps.workers.tests.test_relationships_mysql --settings=config.settings_test_mysql_relationships --noinput -v 2
        if ($LASTEXITCODE -ne 0) { throw 'Fallaron las pruebas MySQL; revisa el resultado anterior.' }
    } finally { Pop-Location }
} finally {
    if ($taskStatusContainer -match '^[a-f0-9]{64}$') {
        $taskStatusLabels = docker inspect --format '{{json .Config.Labels}}' $taskStatusContainer | ConvertFrom-Json
        if ($LASTEXITCODE -eq 0 -and $taskStatusLabels.'gm.task-status-verification' -eq 'true') {
            docker rm --force $taskStatusContainer | Out-Null
            Write-Output 'Contenedor de prueba y base sintética eliminados.'
        }
    }
    foreach ($taskStatusKey in $taskStatusPrevious.Keys) {
        [Environment]::SetEnvironmentVariable($taskStatusKey, $taskStatusPrevious[$taskStatusKey], 'Process')
    }
    $taskStatusPassword = $null
}

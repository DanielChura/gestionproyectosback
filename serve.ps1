param(
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [ValidateRange(1, 64)][int]$Threads = 16,
    [switch]$Warmup
)
$ErrorActionPreference = 'Stop'
$serverRoot = $PSScriptRoot
$serverExecutable = Join-Path $serverRoot '.venv/Scripts/waitress-serve.exe'
if (-not (Test-Path -LiteralPath $serverExecutable)) { throw 'Instala requirements.txt en el entorno del backend antes de iniciar Waitress.' }
$serverListeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners().Port
if ($Port -in $serverListeners) { throw "El puerto $Port está ocupado. Detén explícitamente el servidor correspondiente o usa otro puerto." }
$serverWarmup = $null
try {
    if ($Warmup) {
        $serverWarmup = Start-Job -ArgumentList $Port, $Threads -ScriptBlock {
            param($warmupPort, $warmupThreads)
            Start-Sleep -Seconds 4
            1..$warmupThreads | ForEach-Object -Parallel {
                try { Invoke-WebRequest -Uri "http://127.0.0.1:$using:warmupPort/api/health/" -TimeoutSec 10 | Out-Null } catch { Write-Warning 'No se completó una consulta de precalentamiento.' }
            } -ThrottleLimit $warmupThreads
        }
    }
    Push-Location -LiteralPath $serverRoot
    try { & $serverExecutable --listen="127.0.0.1:$Port" --threads=$Threads config.wsgi:application }
    finally { Pop-Location }
} finally {
    if ($null -ne $serverWarmup) { Stop-Job -Job $serverWarmup; Remove-Job -Job $serverWarmup }
}

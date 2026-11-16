
param(
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [ValidateRange(1, 64)][int]$Threads = 16,
    [switch]$Warmup
)

$ErrorActionPreference = 'Stop'

$serverRoot = $PSScriptRoot
$pythonExecutable = Join-Path $serverRoot '.venv/Scripts/python.exe'

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'No existe el Python del entorno virtual. Crea primero .venv.'
}

& $pythonExecutable -c "import waitress"

if ($LASTEXITCODE -ne 0) {
    throw 'Waitress no está instalado. Ejecuta: .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}

$serverListeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners().Port

if ($Port -in $serverListeners) {
    throw "El puerto $Port está ocupado. Detén explícitamente el servidor correspondiente o usa otro puerto."
}

$serverWarmup = $null

try {
    if ($Warmup) {
        $serverWarmup = Start-Job -ArgumentList $Port, $Threads -ScriptBlock {
            param($warmupPort, $warmupThreads)

            Start-Sleep -Seconds 4

            1..$warmupThreads | ForEach-Object -Parallel {
                try {
                    Invoke-WebRequest -Uri "http://127.0.0.1:$using:warmupPort/api/health/" -TimeoutSec 10 | Out-Null
                }
                catch {
                    Write-Warning 'No se completó una consulta de precalentamiento.'
                }
            } -ThrottleLimit $warmupThreads
        }
    }

    Push-Location -LiteralPath $serverRoot

    try {
        & $pythonExecutable -c "import sys; from waitress import serve; from config.wsgi import application; serve(application, host='127.0.0.1', port=int(sys.argv[1]), threads=int(sys.argv[2]))" "$Port" "$Threads"

        if ($LASTEXITCODE -ne 0) {
            throw "Waitress terminó con código de error $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($null -ne $serverWarmup) {
        Stop-Job -Job $serverWarmup
        Remove-Job -Job $serverWarmup
    }
}

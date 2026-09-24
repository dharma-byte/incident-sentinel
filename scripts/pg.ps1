<#
.SYNOPSIS
    Manage the local portable PostgreSQL server used for development.

.DESCRIPTION
    Docker Desktop could not run on this machine (C: is nearly full), so the
    project uses a standalone PostgreSQL 16 install on D: instead. Binaries and
    data both live outside the repo.

.EXAMPLE
    .\scripts\pg.ps1 init      # one-time: create the cluster, role and databases
    .\scripts\pg.ps1 start
    .\scripts\pg.ps1 status
    .\scripts\pg.ps1 stop
    .\scripts\pg.ps1 psql
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('init', 'start', 'stop', 'restart', 'status', 'psql')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'

$PgRoot = 'D:\pgsql'
$PgData = 'D:\pgsql\data'
$PgLog = 'D:\pgsql\server.log'
$PgUser = 'sentinel'
$PgPassword = 'sentinel'
$Databases = @('incident_sentinel', 'incident_sentinel_test')

$pg_ctl = Join-Path $PgRoot 'bin\pg_ctl.exe'
$initdb = Join-Path $PgRoot 'bin\initdb.exe'
$createdb = Join-Path $PgRoot 'bin\createdb.exe'
$psql = Join-Path $PgRoot 'bin\psql.exe'

if (-not (Test-Path $pg_ctl)) {
    throw "PostgreSQL binaries not found at $PgRoot. Extract the standalone zip there first."
}

$env:PGPASSWORD = $PgPassword

function Test-Running {
    & $pg_ctl -D $PgData status *> $null
    return $LASTEXITCODE -eq 0
}

switch ($Action) {
    'init' {
        if (Test-Path $PgData) { throw "$PgData already exists; delete it first to re-initialise." }
        $pwFile = Join-Path $env:TEMP 'pg_sentinel_pw.txt'
        Set-Content -Path $pwFile -Value $PgPassword -NoNewline -Encoding ascii
        try {
            & $initdb -D $PgData -U $PgUser --pwfile=$pwFile --auth=scram-sha-256 --encoding=UTF8
            if ($LASTEXITCODE -ne 0) { throw 'initdb failed' }
        } finally {
            Remove-Item $pwFile -ErrorAction SilentlyContinue
        }
        & $pg_ctl -D $PgData -l $PgLog start
        Start-Sleep -Seconds 3
        foreach ($db in $Databases) {
            & $createdb -h localhost -U $PgUser $db
            if ($LASTEXITCODE -eq 0) { "created database $db" }
        }
        "cluster ready at $PgData"
    }
    'start' {
        if (Test-Running) { 'already running'; break }
        & $pg_ctl -D $PgData -l $PgLog start
    }
    'stop' {
        if (-not (Test-Running)) { 'not running'; break }
        & $pg_ctl -D $PgData stop -m fast
    }
    'restart' { & $pg_ctl -D $PgData -l $PgLog restart -m fast }
    'status' {
        & $pg_ctl -D $PgData status
        if ($LASTEXITCODE -ne 0) { "server is stopped (start it with: .\scripts\pg.ps1 start)" }
    }
    'psql' { & $psql -h localhost -U $PgUser -d $Databases[0] }
}

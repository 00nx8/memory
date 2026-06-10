# run-claude-proxied.ps1
# Launch Claude Code routed through the local memory-inject proxy, in one step.
#
# The proxy (proxy.py) must already be running in its own window:
#     python proxy.py
#
# Then, instead of `claude`, run:
#     .\run-claude-proxied.ps1            (or pass claude args: .\run-claude-proxied.ps1 --resume)
#
# This sets ANTHROPIC_BASE_URL only for the Claude Code process it spawns --
# it does not leak to your shell. Close Claude Code and the routing is gone.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$portFile = Join-Path $here "proxy_port.txt"

if (-not (Test-Path $portFile)) {
    Write-Host "No proxy_port.txt found -- start the proxy first:  python proxy.py" -ForegroundColor Red
    exit 1
}
$port = (Get-Content $portFile -Raw).Trim()

# Verify the proxy is actually listening before handing Claude Code to it --
# otherwise every request would fail with no obvious cause.
$tcp = New-Object System.Net.Sockets.TcpClient
try {
    $tcp.Connect("127.0.0.1", [int]$port)
    $tcp.Close()
} catch {
    Write-Host "Proxy is not reachable on 127.0.0.1:$port" -ForegroundColor Red
    Write-Host "Start it first (in its own window):  python proxy.py" -ForegroundColor Red
    exit 1
}

# Resolve the project name from the CURRENT working directory and hand it
# to the proxy via a sidecar file. The proxy reads this on every request,
# so the DB tags rows with the right project even though the proxy process
# itself was started before this script ran and doesn't inherit our env.
#
# Convention: if pwd is under ~/projects/<name>/..., project = <name>;
# otherwise 'misc'. Override by setting $env:MEMORY_INJECT_PROJECT before
# running this script.
$projectsRoot = Join-Path $env:USERPROFILE "projects"
$here_pwd = (Get-Location).Path
$project = $env:MEMORY_INJECT_PROJECT
if (-not $project) {
    if ($here_pwd.ToLower().StartsWith($projectsRoot.ToLower())) {
        $rel = $here_pwd.Substring($projectsRoot.Length).TrimStart('\','/')
        $project = ($rel -split '[\\/]')[0]
    }
    if (-not $project) { $project = "misc" }
}
Set-Content -Path (Join-Path $here "current_project.txt") -Value $project -NoNewline
Write-Host "memory-inject project = $project" -ForegroundColor Cyan

$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:$port"
Write-Host "Routing Claude Code through memory-inject proxy at $env:ANTHROPIC_BASE_URL" -ForegroundColor Green
claude @args

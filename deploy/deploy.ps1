<#
.SYNOPSIS
Deploys the last commit of this repository to the hub container on Proxmox.

.DESCRIPTION
Packs the committed files, copies them to the Proxmox host over SSH, pushes
them into the container, and runs deploy/install.sh there. Uncommitted
changes are not deployed. The server needs no access to the Git host.

.EXAMPLE
.\deploy\deploy.ps1 -ProxmoxHost 192.168.178.114 -Container 102
#>
param(
    [Parameter(Mandatory = $true)][string]$ProxmoxHost,
    [Parameter(Mandatory = $true)][int]$Container,
    [string]$User = "root"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $env:TEMP "claude-hub.tar.gz"
$target = "$User@$ProxmoxHost"
$remote = "/tmp/claude-hub.tar.gz"

function Invoke-Step([string]$Label, [scriptblock]$Command) {
    Write-Host "== $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE." }
}

Invoke-Step "Pack the last commit" {
    git -C $repo archive --format=tar.gz -o $archive HEAD
}
Invoke-Step "Copy to the Proxmox host" {
    scp $archive "${target}:$remote"
}

# One SSH call, so that the password is asked only once. The command has no
# quotes inside, because PowerShell changes nested quotes for native programs.
$steps = @(
    "pct push $Container $remote $remote",
    "pct exec $Container -- rm -rf /opt/claude-hub/src",
    "pct exec $Container -- mkdir -p /opt/claude-hub/src",
    "pct exec $Container -- tar -xzf $remote -C /opt/claude-hub/src",
    "pct exec $Container -- sh /opt/claude-hub/src/deploy/install.sh"
) -join " && "
Invoke-Step "Install in container $Container" {
    ssh $target $steps
}

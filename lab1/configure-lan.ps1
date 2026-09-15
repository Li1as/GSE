param(
    [Parameter(Mandatory=$true)]
    [string]$ListenAddress,
    [Parameter(Mandatory=$true)]
    [string]$WslAddress
)
$ErrorActionPreference = 'Stop'
$resultPath = Join-Path $env:TEMP 'GSE-Lab1-portproxy-result.json'
try {
    $null = [System.Net.IPAddress]::Parse($ListenAddress)
    $null = [System.Net.IPAddress]::Parse($WslAddress)
    Start-Service iphlpsvc
    & netsh interface portproxy add v4tov4 listenaddress=$ListenAddress listenport=8765 connectaddress=$WslAddress connectport=8765 protocol=tcp
    if ($LASTEXITCODE -ne 0) { throw 'netsh portproxy failed' }
    $name = 'GSE-Lab1-8765'
    if (Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -Name $name
    }
    New-NetFirewallRule -Name $name -DisplayName 'GSE Lab1 8765' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -LocalAddress $ListenAddress -RemoteAddress LocalSubnet -InterfaceAlias WLAN -Profile Any | Out-Null
    @{ok=$true; listen=$ListenAddress; target=$WslAddress; port=8765} | ConvertTo-Json | Set-Content -Encoding UTF8 $resultPath
} catch {
    @{ok=$false; error=$_.Exception.Message} | ConvertTo-Json | Set-Content -Encoding UTF8 $resultPath
    exit 1
}

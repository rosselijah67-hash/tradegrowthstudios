$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$PublicOutreachDir = Join-Path $ProjectRoot "public_outreach"
$EnvPath = Join-Path $ProjectRoot ".env"
$OutreachConfigPath = Join-Path $ProjectRoot "config\outreach.yaml"

Set-Location $ProjectRoot

function Get-ConfigValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $EnvironmentValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($EnvironmentValue)) {
        return $EnvironmentValue.Trim()
    }

    if (Test-Path $EnvPath) {
        foreach ($Line in Get-Content $EnvPath) {
            $Trimmed = $Line.Trim()
            if ($Trimmed -eq "" -or $Trimmed.StartsWith("#")) {
                continue
            }
            if ($Trimmed -match "^$([regex]::Escape($Name))\s*=\s*(.*)$") {
                return $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }

    if (Test-Path $OutreachConfigPath) {
        foreach ($Line in Get-Content $OutreachConfigPath) {
            $Trimmed = $Line.Trim()
            if ($Trimmed -eq "" -or $Trimmed.StartsWith("#")) {
                continue
            }
            if ($Trimmed -match "^$([regex]::Escape($Name))\s*:\s*(.*)$") {
                return $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }

    return $null
}

Write-Host ""
Write-Host "Deploy Public Audit Packets To Cloudflare Pages"
Write-Host "Project: $ProjectRoot"
Write-Host "Static folder: $PublicOutreachDir"
Write-Host ""

if (-not (Test-Path $PublicOutreachDir)) {
    Write-Error "public_outreach/ was not found. Generate packets first with: python -m src.public_packets --market <market> --limit <limit>"
    exit 1
}

$ProjectName = Get-ConfigValue "PUBLIC_PACKET_PAGES_PROJECT"
if ([string]::IsNullOrWhiteSpace($ProjectName)) {
    Write-Error "PUBLIC_PACKET_PAGES_PROJECT is not configured. Add it to .env or config/outreach.yaml."
    exit 1
}

$BaseUrl = Get-ConfigValue "PUBLIC_PACKET_BASE_URL"
if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    Write-Warning "PUBLIC_PACKET_BASE_URL is not configured. Deployment can continue, but outreach links need this value."
}

$Wrangler = Get-Command wrangler -ErrorAction SilentlyContinue
if (-not $Wrangler) {
    Write-Error "Cloudflare Wrangler was not found. Install it with: npm install -g wrangler"
    Write-Host "Then authenticate with: wrangler login"
    exit 1
}

Write-Host "Cloudflare Pages project: $ProjectName"
if ($BaseUrl) {
    Write-Host "Public packet base URL: $BaseUrl"
}
Write-Host ""
Write-Host "Deploying public_outreach/ only. The local Flask dashboard is not deployed."
Write-Host ""

& wrangler pages deploy public_outreach --project-name $ProjectName

if ($LASTEXITCODE -ne 0) {
    Write-Error "Cloudflare Pages deployment failed. Check the Wrangler output above."
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Deployment finished."
if ($BaseUrl) {
    Write-Host "Open a generated packet using: $BaseUrl/p/<token>/"
}

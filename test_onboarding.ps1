<#
.SYNOPSIS
    Full end-to-end KYC onboarding test against a locally running backend.

.DESCRIPTION
    Runs: health -> start -> document -> [challenge] -> biometric -> complete
    and prints a colour-coded pass/fail summary for every verification check.

.PARAMETER BaseUrl
    Backend base URL. Default: http://localhost:8000

.PARAMETER DocPath
    Path to the identity document image (PNG/JPG).

.PARAMETER VideoPath
    Path to the biometric video clip (MP4).

.PARAMETER FullName
    Applicant full name. Must match the name on the document.

.PARAMETER Nationality
    ISO-3166-1 alpha-2 code. Default: PT

.PARAMETER DocumentType
    'national_id' or 'passport'. Default: national_id

.PARAMETER HolderAddress
    Ethereum address for the DID. Default: Hardhat account #1.

.PARAMETER ChallengeAnswer
    Pre-supply the challenge answer to avoid being prompted.
    Use '-ChallengeAnswer skip' to skip the challenge step entirely.

.NOTES
    The defaults below point at ./samples/. Either drop your own document image
    and video clip there, or pass -DocPath / -VideoPath explicitly.

    For repeated local runs, copy this script to test_onboarding.local.ps1 and
    edit the defaults there -- that filename is git-ignored, so your own paths
    and personal data never reach the repository.

.EXAMPLE
    .\test_onboarding.ps1 -DocPath '.\samples\id.png' -VideoPath '.\samples\clip.mp4'

.EXAMPLE
    .\test_onboarding.ps1 -ChallengeAnswer skip
#>

param(
    [string]$BaseUrl          = "http://localhost:8000",
    [string]$FullName         = "Jane Doe",
    [string]$Nationality      = "PT",
    [string]$DocumentType     = "national_id",
    [string]$HolderAddress    = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    [string]$DocPath          = '.\samples\document.png',
    [string]$VideoPath        = '.\samples\video.mp4',
    [string]$ChallengeAnswer  = ""
)

$ErrorActionPreference = "Stop"

# -- Helpers -----------------------------------------------------------------

function Write-Section($title) {
    $bar = "-" * (54 - $title.Length)
    Write-Host "`n-- $title $bar" -ForegroundColor Cyan
}

function Write-Pass($label) { Write-Host "  [PASS] $label" -ForegroundColor Green }
function Write-Fail($label) { Write-Host "  [FAIL] $label" -ForegroundColor Red }
function Write-Info($label) { Write-Host "  [INFO] $label" -ForegroundColor Gray }
function Write-Warn($label) { Write-Host "  [WARN] $label" -ForegroundColor Yellow }

function Invoke-Json($method, $uri, $body = $null) {
    $params = @{ Method = $method; Uri = $uri; ContentType = "application/json" }
    if ($body) { $params.Body = ($body | ConvertTo-Json) }
    try {
        return Invoke-RestMethod @params
    } catch {
        $detail = $_.ErrorDetails.Message
        if ($detail) {
            try { $detail = ($detail | ConvertFrom-Json).detail } catch {}
        }
        Write-Host "`n  ERROR $($_.Exception.Response.StatusCode.value__): $detail" -ForegroundColor Red
        exit 1
    }
}

function Invoke-Upload($uri, $filePath, $contentType) {
    # curl.exe is bundled with Windows 10/11 and handles multipart cleanly.
    $raw = curl.exe -s -w "`n%{http_code}" -X POST $uri -F "file=@$filePath;type=$contentType"
    $lines    = $raw -split "`n"
    $httpCode = [int]($lines[-1].Trim())
    $body     = ($lines[0..($lines.Length - 2)]) -join "`n"
    if ($httpCode -ge 400) {
        try { $detail = ($body | ConvertFrom-Json).detail } catch { $detail = $body }
        Write-Host "`n  ERROR $httpCode`: $detail" -ForegroundColor Red
        exit 1
    }
    return $body | ConvertFrom-Json
}

function Show-Flag($value, $label, [switch]$InvertPass) {
    $pass = if ($InvertPass) { -not $value } else { $value }
    if ($pass -eq $true)  { Write-Pass $label }
    elseif ($pass -eq $false) { Write-Fail $label }
    else                      { Write-Info "$label (n/a)" }
}

# -- Pre-flight ---------------------------------------------------------------

Write-Section "Pre-flight"

if (-not (Test-Path $DocPath))   { Write-Fail "Document not found: $DocPath";  exit 1 }
if (-not (Test-Path $VideoPath)) { Write-Fail "Video not found: $VideoPath";    exit 1 }
Write-Pass "Document: $DocPath"
Write-Pass "Video:    $VideoPath"

try { $health = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get }
catch { Write-Fail "Backend not reachable at $BaseUrl`n  $($_.Exception.Message)"; exit 1 }
if ($health.status -ne "ok") { Write-Fail "Health returned: $($health.status)"; exit 1 }
Write-Pass "Backend reachable ($BaseUrl)"

# -- Step 1: Start session ----------------------------------------------------

Write-Section "Step 1 -- Start session"
$start = Invoke-Json POST "$BaseUrl/api/v1/onboarding/start" @{
    full_name      = $FullName
    nationality    = $Nationality
    document_type  = $DocumentType
    holder_address = $HolderAddress
}
$session = $start.session_id
Write-Pass "session_id : $session"
Write-Info "holder_did : $($start.holder_did)"

# -- Step 2: Document upload ---------------------------------------------------

Write-Section "Step 2 -- Document upload"
$doc = Invoke-Upload "$BaseUrl/api/v1/onboarding/$session/document" $DocPath "image/png"
Show-Flag $doc.authentic    "Document authentic  (confidence $([math]::Round($doc.confidence,3)))"
if ($doc.flags.Count -gt 0) { Write-Warn "Flags: $($doc.flags -join ', ')" }
Write-Info "OCR fields: $($doc.ocr_data.Keys -join ', ')"

# -- Step 2b: Challenge (Etapa 8) ---------------------------------------------

Write-Section "Step 2b -- Contextual challenge (Etapa 8)"
$ch = Invoke-Json GET "$BaseUrl/api/v1/onboarding/$session/challenge"
if (-not $ch.available) {
    Write-Info "No challenge available for this document (insufficient MRZ data) -- skipped"
} elseif ($ChallengeAnswer -eq "skip") {
    Write-Warn "Challenge skipped by -ChallengeAnswer skip (will not penalise risk score)"
} else {
    Write-Info "Question: $($ch.question)"
    if ($ChallengeAnswer -eq "") {
        $ChallengeAnswer = Read-Host "  Your answer"
    }
    $ans = Invoke-Json POST "$BaseUrl/api/v1/onboarding/$session/challenge" @{ answer = $ChallengeAnswer }
    Show-Flag $ans.correct "Challenge answer"
    Write-Info $ans.detail
}

# -- Step 3: Biometric upload -------------------------------------------------

Write-Section "Step 3 -- Biometric upload (may take 20-60 s)"
Write-Info "Uploading $VideoPath ..."
$bio = Invoke-Upload "$BaseUrl/api/v1/onboarding/$session/biometric" $VideoPath "video/mp4"

Write-Host ""
Write-Info "=== Biometric results ==="

Show-Flag $bio.face_match  "Face match            (score $([math]::Round($bio.match_score,4)))"
Show-Flag $bio.liveness_pass "Liveness (rPPG)      (score $([math]::Round($bio.liveness_score,4)), HR $($bio.liveness_hr_bpm) bpm)"

if ($null -ne $bio.session_quality_pass) {
    Show-Flag $bio.session_quality_pass "Session quality"
    if ($bio.session_quality_flags.Count -gt 0) { Write-Warn "  Flags: $($bio.session_quality_flags -join ', ')" }
}
if ($null -ne $bio.temporal_consistency_pass) {
    Show-Flag $bio.temporal_consistency_pass "Temporal consistency"
    if ($bio.temporal_consistency_flags.Count -gt 0) { Write-Warn "  Flags: $($bio.temporal_consistency_flags -join ', ')" }
}
if ($null -ne $bio.audio_consistency_pass) {
    $audioDetail = "silence $($bio.audio_silence_ratio), flatness $($bio.audio_spectral_flatness)"
    if ($null -ne $bio.audio_lip_sync_corr) { $audioDetail += ", lip_sync $($bio.audio_lip_sync_corr)" }
    Show-Flag $bio.audio_consistency_pass "Audio consistency     ($audioDetail)"
    if ($bio.audio_consistency_flags.Count -gt 0) { Write-Warn "  Flags: $($bio.audio_consistency_flags -join ', ')" }
}

# -- Step 4: Complete ---------------------------------------------------------

Write-Section "Step 4 -- Complete onboarding"
$complete = Invoke-Json POST "$BaseUrl/api/v1/onboarding/$session/complete"

Write-Host ""
Write-Host "  Risk level : " -NoNewline
$riskColor = if ($complete.risk_level -eq "low") { "Green" } elseif ($complete.risk_level -eq "medium") { "Yellow" } else { "Red" }
Write-Host $complete.risk_level.ToUpper() -ForegroundColor $riskColor
Write-Info "Detail     : $($complete.detail)"

Write-Host ""
if ($complete.credential_id) {
    Write-Host "  SUCCESS -- Verifiable Credential issued" -ForegroundColor Green
    Write-Host "  credential_id: $($complete.credential_id)" -ForegroundColor Green
} else {
    Write-Host "  REJECTED -- $($complete.detail)" -ForegroundColor Red
}
Write-Host ""

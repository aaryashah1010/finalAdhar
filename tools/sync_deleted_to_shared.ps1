param(
    [Parameter(Mandatory = $true)]
    [string]$SharedInput,

    [Parameter(Mandatory = $true)]
    [string]$LocalDeleted,

    [Parameter(Mandatory = $true)]
    [string]$SharedOutput,

    [Parameter(Mandatory = $true)]
    [string]$ReportDir
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath([string]$PathValue) {
    return [System.IO.Path]::GetFullPath($PathValue)
}

function Join-RelativePath([string]$Root, [string]$RelativePath) {
    $parts = $RelativePath -split '[\\/]+' | Where-Object { $_ -ne "" }
    $combined = $Root
    foreach ($part in $parts) {
        $combined = Join-Path -Path $combined -ChildPath $part
    }
    return $combined
}

function Get-RelativePathCompat([string]$Root, [string]$Child) {
    $rootFull = Resolve-FullPath $Root
    $childFull = Resolve-FullPath $Child

    if (-not $rootFull.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $rootFull += [System.IO.Path]::DirectorySeparatorChar
    }

    if (-not $childFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside root. Root='$rootFull' Child='$childFull'"
    }

    return $childFull.Substring($rootFull.Length)
}

if (-not (Test-Path -LiteralPath $SharedInput -PathType Container)) {
    throw "Shared input folder does not exist: $SharedInput"
}

if (-not (Test-Path -LiteralPath $LocalDeleted -PathType Container)) {
    throw "Local deleted folder does not exist: $LocalDeleted"
}

New-Item -ItemType Directory -Force -Path $SharedOutput | Out-Null
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$reportPath = Join-Path $ReportDir "sync_deleted_$timestamp.csv"
$rows = New-Object System.Collections.Generic.List[object]

$files = @(Get-ChildItem -LiteralPath $LocalDeleted -File -Recurse)

foreach ($file in $files) {
    $relativePath = Get-RelativePathCompat -Root $LocalDeleted -Child $file.FullName
    $sharedDeletedPath = Join-RelativePath -Root $SharedOutput -RelativePath $relativePath
    $sharedOriginalPath = Join-RelativePath -Root $SharedInput -RelativePath $relativePath

    $status = "UNKNOWN"
    $message = ""

    try {
        $sharedDeletedParent = Split-Path -Parent $sharedDeletedPath
        New-Item -ItemType Directory -Force -Path $sharedDeletedParent | Out-Null

        Copy-Item -LiteralPath $file.FullName -Destination $sharedDeletedPath -Force

        $localHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        $copiedHash = (Get-FileHash -LiteralPath $sharedDeletedPath -Algorithm SHA256).Hash

        if ($localHash -ne $copiedHash) {
            $status = "COPY_HASH_MISMATCH"
            $message = "Copied file hash does not match local deleted file. Original was not removed."
        }
        elseif (-not (Test-Path -LiteralPath $sharedOriginalPath -PathType Leaf)) {
            $status = "COPIED_ORIGINAL_MISSING"
            $message = "Copied to shared output. Matching original was already missing."
        }
        else {
            $originalHash = (Get-FileHash -LiteralPath $sharedOriginalPath -Algorithm SHA256).Hash
            if ($originalHash -ne $localHash) {
                $status = "COPIED_ORIGINAL_CHANGED"
                $message = "Copied to shared output. Original hash changed since staging, so it was not removed."
            }
            else {
                Remove-Item -LiteralPath $sharedOriginalPath -Force
                $status = "COPIED_AND_REMOVED_ORIGINAL"
                $message = "Copied to shared output and removed matching original."
            }
        }
    }
    catch {
        $status = "ERROR"
        $message = $_.Exception.Message
    }

    $rows.Add([pscustomobject]@{
        Status = $status
        RelativePath = $relativePath
        LocalDeleted = $file.FullName
        SharedOutput = $sharedDeletedPath
        SharedOriginal = $sharedOriginalPath
        Message = $message
    }) | Out-Null
}

$rows | Export-Csv -LiteralPath $reportPath -NoTypeInformation

$total = $rows.Count
$removed = @($rows | Where-Object { $_.Status -eq "COPIED_AND_REMOVED_ORIGINAL" }).Count
$copiedOnly = @($rows | Where-Object { $_.Status -like "COPIED_*" -and $_.Status -ne "COPIED_AND_REMOVED_ORIGINAL" }).Count
$errors = @($rows | Where-Object { $_.Status -eq "ERROR" -or $_.Status -eq "COPY_HASH_MISMATCH" }).Count

Write-Host ""
Write-Host "Sync report: $reportPath"
Write-Host "Total deleted files found : $total"
Write-Host "Originals removed        : $removed"
Write-Host "Copied but not removed   : $copiedOnly"
Write-Host "Errors                   : $errors"
Write-Host ""

if ($errors -gt 0) {
    exit 2
}

exit 0

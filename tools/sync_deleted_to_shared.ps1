param(
    [Parameter(Mandatory = $true)]
    [string]$SharedInput,

    [Parameter(Mandatory = $true)]
    [string]$LocalDeleted,

    [Parameter(Mandatory = $true)]
    [string]$SharedOutput,

    [Parameter(Mandatory = $false)]
    [string]$LocalInput = "",

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
$maskedManifestName = "_masked_keep_manifest.json"
$maskedTmpDirName = ".masked_keep_tmp"

$files = @(
    Get-ChildItem -LiteralPath $LocalDeleted -File -Recurse |
        Where-Object {
            $_.Extension -ieq ".pdf" -and
            $_.Name -ne $maskedManifestName -and
            $_.FullName -notmatch [regex]::Escape([System.IO.Path]::DirectorySeparatorChar + $maskedTmpDirName + [System.IO.Path]::DirectorySeparatorChar)
        }
)

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
        Operation = "Delete"
        Status = $status
        RelativePath = $relativePath
        LocalDeleted = $file.FullName
        LocalMasked = ""
        SharedOutput = $sharedDeletedPath
        SharedOriginal = $sharedOriginalPath
        Message = $message
    }) | Out-Null
}

$maskedManifestPath = Join-Path $LocalDeleted $maskedManifestName
$maskedCount = 0
$maskedReplaced = 0

if (Test-Path -LiteralPath $maskedManifestPath -PathType Leaf) {
    if (-not $LocalInput) {
        throw "Masked keep manifest exists, but LocalInput was not provided."
    }
    if (-not (Test-Path -LiteralPath $LocalInput -PathType Container)) {
        throw "Local input folder does not exist: $LocalInput"
    }

    $manifest = Get-Content -Raw -LiteralPath $maskedManifestPath | ConvertFrom-Json
    $maskedFiles = @($manifest.files | Where-Object { $_ -is [string] -and $_ -ne "" })

    foreach ($relativePath in $maskedFiles) {
        $maskedCount += 1
        $localMaskedPath = Join-RelativePath -Root $LocalInput -RelativePath $relativePath
        $sharedOriginalPath = Join-RelativePath -Root $SharedInput -RelativePath $relativePath
        $status = "UNKNOWN"
        $message = ""

        try {
            if (-not (Test-Path -LiteralPath $localMaskedPath -PathType Leaf)) {
                $status = "MASKED_LOCAL_MISSING"
                $message = "Masked local file was not found. Shared original was not replaced."
            }
            elseif (-not (Test-Path -LiteralPath $sharedOriginalPath -PathType Leaf)) {
                $status = "MASKED_ORIGINAL_MISSING"
                $message = "Shared original was not found. Nothing was replaced."
            }
            else {
                $sharedParent = Split-Path -Parent $sharedOriginalPath
                New-Item -ItemType Directory -Force -Path $sharedParent | Out-Null

                $tempName = "." + [System.IO.Path]::GetFileName($sharedOriginalPath) + ".masked_tmp"
                $sharedTempPath = Join-Path $sharedParent $tempName
                Copy-Item -LiteralPath $localMaskedPath -Destination $sharedTempPath -Force

                $localHash = (Get-FileHash -LiteralPath $localMaskedPath -Algorithm SHA256).Hash
                $tempHash = (Get-FileHash -LiteralPath $sharedTempPath -Algorithm SHA256).Hash

                if ($localHash -ne $tempHash) {
                    Remove-Item -LiteralPath $sharedTempPath -Force -ErrorAction SilentlyContinue
                    $status = "MASKED_COPY_HASH_MISMATCH"
                    $message = "Temporary masked copy hash did not match. Shared original was not replaced."
                }
                else {
                    Move-Item -LiteralPath $sharedTempPath -Destination $sharedOriginalPath -Force
                    $replacedHash = (Get-FileHash -LiteralPath $sharedOriginalPath -Algorithm SHA256).Hash
                    if ($replacedHash -ne $localHash) {
                        $status = "MASKED_REPLACE_HASH_MISMATCH"
                        $message = "Shared original was replaced but final hash did not match local masked file."
                    }
                    else {
                        $status = "MASKED_AND_REPLACED_ORIGINAL"
                        $message = "Shared original was replaced with masked PDF."
                        $maskedReplaced += 1
                    }
                }
            }
        }
        catch {
            $status = "ERROR"
            $message = $_.Exception.Message
        }

        $rows.Add([pscustomobject]@{
            Operation = "MaskKeep"
            Status = $status
            RelativePath = $relativePath
            LocalDeleted = ""
            LocalMasked = $localMaskedPath
            SharedOutput = ""
            SharedOriginal = $sharedOriginalPath
            Message = $message
        }) | Out-Null
    }
}

$rows | Export-Csv -LiteralPath $reportPath -NoTypeInformation

$total = $files.Count
$removed = @($rows | Where-Object { $_.Operation -eq "Delete" -and $_.Status -eq "COPIED_AND_REMOVED_ORIGINAL" }).Count
$copiedOnly = @($rows | Where-Object { $_.Operation -eq "Delete" -and $_.Status -like "COPIED_*" -and $_.Status -ne "COPIED_AND_REMOVED_ORIGINAL" }).Count
$errors = @(
    $rows | Where-Object {
        $_.Status -eq "ERROR" -or
        $_.Status -eq "COPY_HASH_MISMATCH" -or
        ($_.Status -like "MASKED_*" -and $_.Status -ne "MASKED_AND_REPLACED_ORIGINAL")
    }
).Count

Write-Host ""
Write-Host "Sync report: $reportPath"
Write-Host "Total deleted files found : $total"
Write-Host "Originals removed        : $removed"
Write-Host "Copied but not removed   : $copiedOnly"
Write-Host "Masked files found       : $maskedCount"
Write-Host "Masked originals updated : $maskedReplaced"
Write-Host "Errors                   : $errors"
Write-Host ""

if ($errors -gt 0) {
    exit 2
}

exit 0

<#
.SYNOPSIS
  将挂载运行时物料（WeChatRPA + matrixmedia）打成独立物料包，供制品仓按版本发布。

.DESCRIPTION
  - 读取 build\material-manifest.json 中的 contents / exclude_patterns；
  - 按排除规则剔除日志、截图、记忆、事件、缓存、个人状态、探针脚本；
  - 计算每个文件的 SHA256，回填 manifest.files；
  - 输出 build\material-<version>.zip 与 build\material-<version>.manifest.json。

  物料仓发布方式：把 zip + manifest 作为一个 Release 附件上传，tag 即 material_version。

.PARAMETER MaterialVersion
  物料版本号，默认取 build\version.json 里的 runtime_material_version。

.PARAMETER SourceDir
  主仓根目录（含 apps\、appr\），默认脚本所在目录的上一级。

.EXAMPLE
  pwsh -File scripts\pack-material.ps1
  pwsh -File scripts\pack-material.ps1 -MaterialVersion rpa-matrix-1.0.0
#>
param(
  [string]$MaterialVersion = "",
  [string]$SourceDir = ""
)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
$root = if ($SourceDir) { $SourceDir } else { Split-Path -Parent $scriptDir }
$buildDir = Join-Path $root "build"

$versionPath = Join-Path $buildDir "version.json"
$manifestPath = Join-Path $buildDir "material-manifest.json"

if (-not (Test-Path $manifestPath)) { throw "缺少 $manifestPath" }

if (Test-Path $versionPath) {
  $version = Get-Content $versionPath -Raw -Encoding UTF8 | ConvertFrom-Json
  if (-not $MaterialVersion) { $MaterialVersion = $version.runtime_material_version }
}
if (-not $MaterialVersion) { $MaterialVersion = "dev" }

$manifest = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$manifest.material_version = $MaterialVersion
$manifest.built_at = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")

# 排除规则转通配
$exclude = @($manifest.exclude_patterns)

function Test-Excluded([string]$relPath) {
  $rel = $relPath -replace '\\', '/'
  foreach ($pat in $exclude) {
    $pattern = $pat -replace '\\', '/'
    # **/ 可匹配零个或多个目录段；其余 * 匹配单段内任意字符
    $regex = [regex]::Escape($pattern)
    $regex = $regex -replace '\\\*\\\*/', '(.*/)?'
    $regex = $regex -replace '\\\*\\\*', '.*'
    $regex = $regex -replace '\\\*', '[^/]*'
    $regex = "^$regex$"
    if ($rel -match $regex) { return $true }
  }
  return $false
}

$fileEntries = @()
$staging = Join-Path $buildDir ("material-stage-" + $MaterialVersion)
if (Test-Path $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $staging | Out-Null

foreach ($item in $manifest.contents) {
  $srcFull = Join-Path $root $item.source
  if (-not (Test-Path $srcFull)) {
    Write-Warning "物料源不存在，跳过：$($item.source)"
    continue
  }
  $destBase = Join-Path $staging $item.dest
  New-Item -ItemType Directory -Force -Path $destBase | Out-Null

  Get-ChildItem -LiteralPath $srcFull -Recurse -File -Force | ForEach-Object {
    $rel = $_.FullName.Substring($srcFull.Length).TrimStart('\', '/')
    if (Test-Excluded $rel) { return }

    $destFull = Join-Path $destBase $rel
    $destParent = Split-Path -Parent $destFull
    New-Item -ItemType Directory -Force -Path $destParent | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $destFull -Force

    $hash = (Get-FileHash -LiteralPath $destFull -Algorithm SHA256).Hash.ToLower()
    $fileEntries += [pscustomobject]@{
      path    = ($item.dest + "/" + ($rel -replace '\\', '/'))
      size    = $_.Length
      sha256  = $hash
    }
  }
}

$manifest.files = $fileEntries

# 写 manifest（随包一起发布，便于校验）
$manifestOut = Join-Path $buildDir ("material-" + $MaterialVersion + ".manifest.json")
$manifest | ConvertTo-Json -Depth 8 | Out-File -LiteralPath $manifestOut -Encoding utf8

# 打 zip
$zipOut = Join-Path $buildDir ("material-" + $MaterialVersion + ".zip")
if (Test-Path $zipOut) { Remove-Item -LiteralPath $zipOut -Force }
Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $zipOut -CompressionLevel Optimal

$zipHash = (Get-FileHash -LiteralPath $zipOut -Algorithm SHA256).Hash.ToLower()
$zipSize = (Get-Item -LiteralPath $zipOut).Length

$zipSizeMB = [math]::Round($zipSize / 1MB, 1)
Write-Host ""
Write-Host "=== 物料包打包完成 ==="
Write-Host "版本        : $MaterialVersion"
Write-Host "文件数      : $($fileEntries.Count)"
Write-Host "zip 路径    : $zipOut"
Write-Host "zip 大小    : $zipSizeMB MB"
Write-Host "zip SHA256  : $zipHash"
Write-Host "清单路径    : $manifestOut"

# 清理暂存目录（保留 zip 与 manifest）
Remove-Item -LiteralPath $staging -Recurse -Force

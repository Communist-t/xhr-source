# ============================================================
# 本机下载物料 Release 附件（绕过本机 DNS 污染 / 跨境直连慢）
#
# 背景：本机 DNS 把 release-assets.githubusercontent.com 解析到 127.0.0.1，
#       且 github.com / codeload 存在错误解析。CI 在海外网络用 gh 即可，
#       本脚本仅用于本机拉取物料包，做三件事：
#         1. 通过直连 --resolve 绑定到可达的 GitHub 真实 IP；
#         2. 直连不通时回退到公共 GitHub 加速镜像；
#         3. 下载后按 manifest / 已知 SHA256 校验完整性。
#
# 用法：
#   pwsh -File scripts\download-material-local.ps1
#   pwsh -File scripts\download-material-local.ps1 -MaterialVersion rpa-matrix-1.0.0 -OutDir build\material-stage
#
# 可选参数：
#   -MaterialVersion  物料 tag，默认取 build\version.json 的 runtime_material_version
#   -Repo              物料仓 owner/name，默认 Communist-t/xhr-materials
#   -OutDir            解压目录，默认 build\material-stage
#   -UseMirrorOnly     强制只用加速镜像（不走直连 IP 绑定）
# ============================================================
param(
  [string]$MaterialVersion = "",
  [string]$Repo = "Communist-t/xhr-materials",
  [string]$OutDir = "",
  [switch]$UseMirrorOnly
)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
$root = Split-Path -Parent $scriptDir

# 读取版本
$versionPath = Join-Path $root "build\version.json"
if (-not $MaterialVersion) {
  if (Test-Path $versionPath) {
    $v = Get-Content $versionPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $MaterialVersion = $v.runtime_material_version
  }
}
if (-not $MaterialVersion) { $MaterialVersion = "rpa-matrix-1.0.0" }
if (-not $OutDir) { $OutDir = Join-Path $root "build\material-stage" }

# 可达 IP（本机实测 2026-11-18；若失效可重新通过 DoH 解析更新）
$ghIps = @("20.247.166.64", "20.205.243.165", "20.205.243.168")
$assetIps = @("185.199.111.133", "185.199.108.133", "185.199.110.133", "185.199.109.133")
$mirrors = @("https://ghfast.top/", "https://ghproxy.net/")

$assetName = "material-$MaterialVersion.zip"
$assetUrl = "https://github.com/$Repo/releases/download/$MaterialVersion/$assetName"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$zipPath = Join-Path $OutDir $assetName

function Resolve-SignedLocation {
  param([string]$Url)
  # 直连绑定 github IP，取 302 的签名 Location
  foreach ($ip in $ghIps) {
    $hdr = Join-Path $env:TEMP ("ghhdr-" + [guid]::NewGuid().ToString("N") + ".txt")
    try {
      curl.exe -s --noproxy "*" --connect-timeout 10 --max-time 20 `
        --resolve "github.com:443:$ip" -D $hdr -o NUL $Url
      $locLine = (Select-String -Path $hdr -Pattern "^Location:" -ErrorAction SilentlyContinue | Select-Object -First 1).Line
      if ($locLine) { return ($locLine -replace "^\s*Location:\s*", "").Trim() }
    } finally { Remove-Item $hdr -Force -ErrorAction SilentlyContinue }
  }
  return $null
}

function Download-WithResolve {
  param([string]$LocationUrl, [string]$OutFile)
  foreach ($ip in $assetIps) {
    Write-Host "  尝试直连对象存储 IP $ip ..."
    $code = curl.exe -sL --noproxy "*" --connect-timeout 10 --max-time 600 `
      --resolve "release-assets.githubusercontent.com:443:$ip" `
      -o $OutFile -w "%{http_code}|%{size_download}|%{speed_download}" $LocationUrl
    $parts = $code -split '\|'
    if ($parts[0] -eq "200" -or $parts[0] -eq "206") {
      $size = [long]$parts[1]
      if ($size -gt 1MB) { Write-Host "  直连成功 ($([math]::Round($size/1MB,1)) MB)" ; return $true }
    }
    Write-Host "  IP $ip 未取到完整文件 ($code)"
  }
  return $false
}

function Download-WithMirror {
  param([string]$Url, [string]$OutFile)
  foreach ($m in $mirrors) {
    $proxyUrl = "$m$Url"
    Write-Host "  尝试镜像 $($m.TrimEnd('/')) ..."
    $code = curl.exe -sL --noproxy "*" --connect-timeout 10 --max-time 600 `
      -o $OutFile -w "%{http_code}|%{size_download}|%{speed_download}" $proxyUrl
    $parts = $code -split '\|'
    if ($parts[0] -eq "200" -or $parts[0] -eq "206") {
      $size = [long]$parts[1]
      if ($size -gt 1MB) { Write-Host "  镜像成功 ($([math]::Round($size/1MB,1)) MB)" ; return $true }
    }
    Write-Host "  镜像 $($m.TrimEnd('/')) 未取到完整文件 ($code)"
  }
  return $false
}

$ok = $false
if (-not $UseMirrorOnly) {
  $loc = Resolve-SignedLocation -Url $assetUrl
  if ($loc) { $ok = Download-WithResolve -LocationUrl $loc -OutFile $zipPath }
}
if (-not $ok) {
  $ok = Download-WithMirror -Url $assetUrl -OutFile $zipPath
}

if (-not $ok) { throw "所有下载方式均失败：$assetUrl" }

# 校验：经 API 取 Release asset 的 digest（sha256:...）比对；取不到则只校验大小
$apiIps = @("20.205.243.168", "20.205.243.166")
$expectedSha = $null
foreach ($ip in $apiIps) {
  try {
    $relJson = Join-Path $env:TEMP ("ghrel-" + [guid]::NewGuid().ToString("N") + ".json")
    curl.exe -s --noproxy "*" --connect-timeout 10 --max-time 20 `
      --resolve "api.github.com:443:$ip" -o $relJson `
      "https://api.github.com/repos/$Repo/releases/tags/$MaterialVersion"
    $rel = Get-Content $relJson -Raw -Encoding UTF8 | ConvertFrom-Json
    $asset = $rel.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
    if ($asset -and $asset.digest -like "sha256:*") { $expectedSha = ($asset.digest -replace "^sha256:", "").ToLower() ; $expectedSize = [long]$asset.size }
    Remove-Item $relJson -Force -ErrorAction SilentlyContinue
    if ($expectedSha) { break }
  } catch { }
}

$actual = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLower()
if ($expectedSha) {
  if ($actual -ne $expectedSha) { throw "SHA256 校验失败：期望 $expectedSha 实际 $actual" }
  if ($expectedSize -and (Get-Item $zipPath).Length -ne $expectedSize) { throw "文件大小不符：期望 $expectedSize 实际 $((Get-Item $zipPath).Length)" }
  Write-Host "SHA256 与大小校验通过"
} else {
  Write-Host "未取到 API digest，跳过哈希校验（当前大小 $([math]::Round((Get-Item $zipPath).Length/1MB,1)) MB）"
}

Expand-Archive -LiteralPath $zipPath -DestinationPath $OutDir -Force
Remove-Item $zipPath -Force
Write-Host "物料已解压到 $OutDir"
Get-ChildItem $OutDir | Select-Object Name

<#
.SYNOPSIS
  xHR Launcher 一键发布：dotnet publish 壳子 → NSIS 安装包；子程序员工物料单独打包并生成物料清单。

.DESCRIPTION
  产物分两类（安装包与物料包分离，安装包保持轻量，物料按需下载）：
    1) 安装包 xHR_<版本>_win-x64-setup.exe：NSIS 安装器，仅 .NET 启动器本体
       （不含 apps\、appr\ 子程序本体）。需本机已安装 NSIS 3（choco install nsis）。
    2) 物料包 material_<物料id>_<物料版本>.zip：每个子程序员工一个包，由物料目录解压后打包。
       物料清单 materials.json 记录各物料版本、下载直链（占位，由工作流回填）、哈希、入口。

  物料来源：-MaterialDir <目录>（CI/本地已解压好的物料目录，内含 apps\、appr\）；
  不传则回退源码树内 apps\、appr\（仅本机联调用，公开仓已被 .gitignore 排除）。

.PARAMETER MaterialDir
  解压后的物料根目录（其下应有 apps\ 与 appr\）。

.PARAMETER Version
  覆盖壳子发布版本号；默认取 build\version.json 的 launcher_version。

.PARAMETER Configuration
  Release / Debug，默认 Release。

.EXAMPLE
  pwsh -File scripts\publish.ps1
  pwsh -File scripts\publish.ps1 -MaterialDir .\build\material-stage
#>
param(
  [string]$MaterialDir = "",
  [string]$Version = "",
  [string]$Configuration = "Release",
  [switch]$WithMaterials
)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
$root = Split-Path -Parent $scriptDir
$buildDir = Join-Path $root "build"
$distDir  = Join-Path $root "dist"

$versionPath = Join-Path $buildDir "version.json"
if (-not (Test-Path $versionPath)) { throw "缺少 $versionPath" }
$versionInfo = Get-Content $versionPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Version) { $Version = $versionInfo.launcher_version }
$materialVersion  = $versionInfo.runtime_material_version
$rid               = $versionInfo.runtime_rid

# 物料槽位 → 物料标识 / 入口定义。入口相对物料解压根目录；工作目录可空（默认入口所在目录）。
# 与客户端 MountedApp.MaterialId 一一对应。
$materialSlots = @(
  [pscustomobject]@{
    Slot       = "apps\WeChatRPA"
    MaterialId = "wechat-rpa"
    EntryExe   = "runtime\python\pythonw.exe"
    EntryArgs  = "gui.py"
    WorkDir    = "runtime"
  },
  [pscustomobject]@{
    Slot       = "appr\matrixmedia"
    MaterialId = "matrix-video"
    EntryExe   = "matrixmedia.exe"
    EntryArgs  = ""
    WorkDir    = ""
  }
)

# ---- 1. 准备干净的壳子发布输出目录 ----
$publishOut = Join-Path $buildDir ("publish-" + $Version)
if (Test-Path $publishOut) { Remove-Item -LiteralPath $publishOut -Recurse -Force }
New-Item -ItemType Directory -Force -Path $publishOut | Out-Null

# ---- 2. dotnet publish（壳子本体）----
Write-Host "==> dotnet publish ($Configuration, $rid)"
$publishArgs = @(
  "publish",
  (Join-Path $root "Launcher.csproj"),
  "-c", $Configuration,
  "-r", $rid,
  "--self-contained", "true",
  "-o", $publishOut,
  "-p:PublishSingleFile=true",
  "-p:IncludeNativeLibrariesForSelfExtract=true",
  "-p:EnableCompressionInSingleFile=true",
  "-v", "minimal"
)
& dotnet @publishArgs
if ($LASTEXITCODE -ne 0) { throw "dotnet publish 失败，退出码 $LASTEXITCODE" }

# 轻量壳包剔除子程序本体；-WithMaterials 出全量包则保留（dotnet publish 已随 csproj 带入）。
if ($WithMaterials) {
  foreach ($slot in @("apps", "appr")) {
    if (-not (Test-Path (Join-Path $publishOut $slot))) {
      throw "全量安装包需要物料，但发布输出缺少 $slot（publish 前物料目录 apps\、appr\ 需就位）"
    }
  }
  # 与物料打包同口径：清除个人状态/运行态文件
  $fullCleanPatterns = @("my_contacts.json", "chat_strategy_config.json", "moments_queue.json",
    "downloaded_skills.json", "user_config.local.json", "*.pid", "__pycache__")
  foreach ($pat in $fullCleanPatterns) {
    Get-ChildItem -LiteralPath $publishOut -Recurse -Force -ErrorAction SilentlyContinue -Filter $pat |
      Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
  }
} else {
  foreach ($slot in @("apps", "appr")) {
    $slotPath = Join-Path $publishOut $slot
    if (Test-Path $slotPath) {
      try {
        [IO.Directory]::Delete($slotPath, $true)
      } catch {
        # 删除不可用（回收站包装/权限）时改名让位，旧目录不再进入发布产物
        Rename-Item -LiteralPath $slotPath -NewName "$slot.old-$(Get-Date -Format yyyyMMddHHmmss)"
      }
    }
  }
}

# ---- 3. 清洗壳包里的运行态 / 个人状态 / 探针脚本 ----
$cleanGlobs = @(
  "**/logs/**", "**/screenshots/**", "**/conversation_memory/**",
  "**/events/**", "**/__pycache__/**", "**/_crops/**", "**/_probe_tmp/**",
  "**/*.pid", "**/_probe_*.py", "**/_test_*.py", "**/_regress_*.py",
  "**/probe_uia.py", "**/calibrate_*.py", "**/*.bak", "**/*.bak2", "**/*.bak3",
  "**/my_contacts.json", "**/chat_strategy_config.json", "**/moments_queue.json",
  "**/downloaded_skills.json", "**/user_config.local.json"
)
foreach ($glob in $cleanGlobs) {
  Get-ChildItem -LiteralPath $publishOut -Recurse -Force -File -Filter ($glob -replace '^\*\*/', '') -ErrorAction SilentlyContinue |
    Where-Object { $rel = $_.FullName.Substring($publishOut.Length); ($glob -replace '^\*\*/', '') -notmatch '[\*\[\]]' -or $_.Name -like ($glob -replace '^\*\*/', '') } |
    Remove-Item -Force -ErrorAction SilentlyContinue
}

# ---- 4. 生成壳子文件清单 ----
$fileEntries = @()
Get-ChildItem -LiteralPath $publishOut -Recurse -File -Force | ForEach-Object {
  $rel = $_.FullName.Substring($publishOut.Length).TrimStart('\', '/') -replace '\\', '/'
  $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower()
  $fileEntries += [pscustomobject]@{ path = $rel; size = $_.Length; sha256 = $hash }
}

# 把版本号文件写进壳子发布目录，供客户端读取本地版本做更新比对。
# launcher_version 必须覆盖为本次实际发布版本（-Version 可能不同于 build\version.json）。
$versionSrc = Join-Path $buildDir "version.json"
if (Test-Path $versionSrc) {
  $outVersion = $versionInfo.PSObject.Copy()
  $outVersion.launcher_version = $Version
  [System.IO.File]::WriteAllText(
    (Join-Path $publishOut "version.json"),
    ($outVersion | ConvertTo-Json -Depth 4),
    (New-Object System.Text.UTF8Encoding $false))
}

# ---- 5. 编译 NSIS 安装包 ----
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
$makensis = @(
  "${env:ProgramFiles(x86)}\NSIS\makensis.exe"
  "$env:ProgramFiles\NSIS\makensis.exe"
  "$env:LOCALAPPDATA\Programs\NSIS\makensis.exe"
  (Join-Path $buildDir "tools\nsis\ucrt64\bin\makensis.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $makensis) { throw "未找到 NSIS（makensis.exe），请先安装：choco install nsis" }

# VIProductVersion 只接受纯数字四段，取版本号前三段补 .0
$verNum = (($Version -split '\.')[0..2] -join '.') + '.0'
$suffix = if ($WithMaterials) { "_full" } else { "" }
$zipName = "xHR_" + $Version + $suffix + "_win-x64-setup.exe"
& $makensis "/DAppVer=$Version" "/DVerNum=$verNum" "/DOutName=$zipName" "/DPublishDir=$publishOut" (Join-Path $root "installer\xhr.nsi")
if ($LASTEXITCODE -ne 0) { throw "makensis 编译失败，退出码 $LASTEXITCODE" }

$zipOut  = Join-Path $distDir $zipName
if (-not (Test-Path -LiteralPath $zipOut)) { throw "安装包未生成：$zipOut" }

$zipHash = (Get-FileHash -LiteralPath $zipOut -Algorithm SHA256).Hash.ToLower()
$zipSize = (Get-Item -LiteralPath $zipOut).Length
$zipSizeMB = [math]::Round($zipSize / 1MB, 1)

"$zipHash  $zipName" | Out-File -LiteralPath "$zipOut.sha256" -Encoding ascii

$manifest = [pscustomobject]@{
  launcher_version        = $Version
  runtime_material_version = $materialVersion
  rid                     = $rid
  published_at            = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
  file_count              = $fileEntries.Count
  files                   = $fileEntries
}
# PS5 的 Out-File -Encoding utf8 带 BOM，客户端 System.Text.Json 解析会失败，统一无 BOM 写盘
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $distDir ("release-manifest_" + $Version + $suffix + ".json")), ($manifest | ConvertTo-Json -Depth 8), $utf8NoBom)

# 壳子更新清单 latest.json 只描述轻量包（自动更新通道）；全量包不占用更新通道。
if (-not $WithMaterials) {
  $updateInfo = [pscustomobject]@{
    launcher_version          = $Version
    package_url               = "__PACKAGE_URL__"
    sha256                    = $zipHash
    size                      = $zipSize
    release_notes             = ""
    mandatory                 = $false
    runtime_material_version  = $materialVersion
  }
  [System.IO.File]::WriteAllText((Join-Path $distDir "latest.json"), ($updateInfo | ConvertTo-Json -Depth 4), $utf8NoBom)
}

# ---- 6. 打包子程序物料并生成物料清单 ----
$materialSource = if ($MaterialDir) {
  [System.IO.Path]::GetFullPath((Join-Path $root $MaterialDir))
} else { $root }

$materialEntries = @()
foreach ($slot in $materialSlots) {
  $srcSlot = Join-Path $materialSource $slot.Slot
  if (-not (Test-Path $srcSlot)) {
    Write-Warning "物料槽位不存在，跳过：$srcSlot"
    continue
  }

  # 物料打包前清洗运行态 / 个人状态文件。
  $statePatterns = @("my_contacts.json", "chat_strategy_config.json", "moments_queue.json",
    "downloaded_skills.json", "user_config.local.json", "*.pid", "__pycache__")
  foreach ($pat in $statePatterns) {
    Get-ChildItem -LiteralPath $srcSlot -Recurse -Force -ErrorAction SilentlyContinue -Filter $pat |
      Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
  }

  $matZipName = "material_" + $slot.MaterialId + "_" + $materialVersion + ".zip"
  $matZipOut = Join-Path $distDir $matZipName
  if (Test-Path $matZipOut) { Remove-Item -LiteralPath $matZipOut -Force }
  Compress-Archive -Path (Join-Path $srcSlot "*") -DestinationPath $matZipOut -CompressionLevel Optimal

  $matHash = (Get-FileHash -LiteralPath $matZipOut -Algorithm SHA256).Hash.ToLower()
  $matSize = (Get-Item -LiteralPath $matZipOut).Length

  $materialEntries += [pscustomobject]@{
    material_id      = $slot.MaterialId
    material_version = $materialVersion
    package_url      = "__MATERIAL_URL_" + $slot.MaterialId + "__"
    sha256           = $matHash
    size             = $matSize
    entry_exe        = $slot.EntryExe
    entry_args       = $slot.EntryArgs
    work_dir         = $slot.WorkDir
  }
  Write-Host "物料包: $matZipName  ($([math]::Round($matSize/1MB,1)) MB, SHA256 $matHash)"
}

$materialManifest = [pscustomobject]@{
  material_version = $materialVersion
  materials        = $materialEntries
}
[System.IO.File]::WriteAllText((Join-Path $distDir "materials.json"), ($materialManifest | ConvertTo-Json -Depth 6), $utf8NoBom)

Write-Host ""
Write-Host "=== 发布完成 ==="
Write-Host "壳子版本     : $Version"
Write-Host "物料版本     : $materialVersion"
Write-Host "安装包       : $zipOut ($zipSizeMB MB)"
Write-Host "安装包 SHA256: $zipHash"
Write-Host "物料清单     : dist\materials.json"

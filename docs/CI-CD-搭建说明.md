# x.HR Launcher —— 双仓 CI/CD 搭建说明

## 一、仓库划分

| 仓库 | 内容 | 不进仓库的东西 |
|---|---|---|
| **主仓**（本仓，源码仓） | WPF 源码、`Assets`、工程文件、`scripts/`、`.github/workflows/`、`build/` 版本与清单骨架 | 运行时物料、构建产物、个人状态、密钥 |
| **物料仓**（独立制品仓） | 打包脚本、`build/version.json`、`material-manifest.json`、README、校验工作流；**物料二进制不入库**，打成 zip 挂到物料 Release 附件 | 物料本体（apps/appr）、日志、截图、记忆、事件、缓存、探针脚本、个人状态 |

主仓流水线按 `build/version.json` 里的 `runtime_material_version` 去物料仓对应 Release 拉取物料 zip，再 `dotnet publish` 出绿色免安装包。

## 二、目录与关键文件

```
Launcher/                        主仓
├── .github/workflows/
│   └── build-release.yml         主仓：拉物料 → publish → 出包 → 发 Release
├── build/
│   ├── version.json              launcher_version / runtime_material_version
│   └── material-manifest.json    物料内容与排除规则
├── scripts/
│   ├── publish.ps1               本机/CI 一键发布
│   ├── pack-material.ps1         物料打包
│   └── download-material-local.ps1  本机绕过 DNS 污染下载物料附件
├── Services/ Models/ ViewModels/ Views/ Converters/ Assets/ ...
└── Launcher.csproj
```

物料仓内目录（Git 内只有骨架，本体走 Release 附件）：

```
xhr-materials/
├── build/version.json
├── build/material-manifest.json
├── scripts/pack-material.ps1
├── .github/workflows/material-check.yml  骨架完整性校验
└── README.md
```

## 三、本地先跑通（已验证）

```powershell
# 1. 打物料包（产物在 build/material-<version>.zip + .manifest.json）
pwsh -File scripts\pack-material.ps1

# 2. 解压物料到暂存目录，再出主程序包
Expand-Archive build\material-rpa-matrix-1.0.0.zip -DestinationPath build\material-stage -Force
pwsh -File scripts\publish.ps1 -MaterialDir build\material-stage
# 产物：dist\x.HR_<版本>_win-x64.zip + .sha256 + release-manifest_<版本>.json
```

本机无 .NET 9 运行时时，可设 `$env:DOTNET_ROLL_FORWARD='LatestMajor'` 用 SDK 10 构建；CI 用 `actions/setup-dotnet` 装 9.0.x。

## 四、两个 GitHub 仓库的创建与推送

### 1. 物料仓 `xhr-materials`

```powershell
# 在物料物料目录准备物料仓内容（示例：从主仓复制运行时物料与打包脚本）
git init
git add apps appr build/material-manifest.json scripts/pack-material.ps1
git commit -m "chore: 初始化物料仓 rpa-matrix-1.0.0"
git branch -M main
git remote add origin https://github.com/Communist-t/xhr-materials.git
git push -u origin main

# 打物料版本 tag 并推送，触发物料流水线打包发 Release
git tag rpa-matrix-1.0.0
git push origin rpa-matrix-1.0.0
```

> 物料仓若设为私有，主仓需要一个有读权限的 PAT（见下）。

### 2. 主仓

```powershell
cd D:\copyofthefile\x.hr\Launcher
# 确认 .gitignore 已排除 apps/appr/bin/obj/dist/build 暂存产物后再提交
git init
git add .gitignore .github build scripts Services Models ViewModels Views Converters Assets `
      App.xaml App.xaml.cs AssemblyInfo.cs Launcher.csproj MainWindow.xaml MainWindow.xaml.cs app.ico
git commit -m "chore: 初始化主仓"
git branch -M main
git remote add origin https://github.com/Communist-t/xhr-launcher.git
git push -u origin main

# 发正式版：打 v 标签触发主仓流水线出 Release
git tag v0.1.0
git push origin v0.1.0
```

## 五、仓库变量与密钥配置（主仓 Settings）

| 类型 | 名称 | 说明 |
|---|---|---|
| Variables | `MATERIAL_REPO` | 物料仓地址，格式 `Communist-t/xhr-materials`（默认已填，可不改） |
| Secrets | `MATERIAL_TOKEN` | 物料仓私有时填只读 PAT；物料仓公开可留空（流水线回退用 `GITHUB_TOKEN`） |

流水线默认物料仓占位为 `Communist-t/xhr-materials`，与本说明一致；如仓名调整，在主仓 Variables 里覆盖。

## 六、版本对应关系

- 主仓 tag：`v0.1.0` → `launcher_version=0.1.0`
- 物料 tag：`rpa-matrix-1.0.0` → `runtime_material_version=rpa-matrix-1.0.0`
- 主仓 `build/version.json` 的物料版本要与物料仓最新 tag 一致；子程序升级时单独给物料仓打新 tag，再回主仓更新该版本号。

## 七、本机下载 GitHub Release 附件（DNS 污染排查）

### 现象

本机直连下载物料/产物附件时卡在 302 之后：`github.com`、`api.github.com`、`objects.githubusercontent.com` 可达，但 `release-assets.githubusercontent.com` 被本机 DNS 解析到 `127.0.0.1`/`::1`，对象存储 443 不通；`codeload.github.com` 也被解析到错误的 `57.155.x.x`。CI 在海外网络不受影响（云端下载物料步骤已 success）。

### 已实测结论（2026-11-18）

- 真实 IP 可通过 DoH 获取：`https://dns.alidns.com/resolve?name=<域名>&type=A`（Accept: `application/dns-json`）。
- 当前可达：`github.com → 20.247.166.64`；`release-assets → 185.199.108–111.133`；`api.github.com → 20.205.243.168`。
- 直连跨境带宽受限：单连接 7–66 KB/s，4 连接并行约 89 KB/s；公共镜像 `ghfast.top` 单连接约 70 KB/s，并发会被限流。大文件本机下载仍偏慢，正式分发应走对象存储/自有下载页。

### 本机下载脚本

`scripts/download-material-local.ps1`：先直连绑定可达 IP 取签名地址下载，失败回退加速镜像，最后经 API 取 asset `digest` 校验 SHA256 与大小。

```powershell
# 默认读 build/version.json 的物料版本，解压到 build\material-stage
pwsh -File scripts\download-material-local.ps1

# 指定版本 / 目录 / 强制走镜像
pwsh -File scripts\download-material-local.ps1 -MaterialVersion rpa-matrix-1.0.0 -OutDir build\material-stage
pwsh -File scripts\download-material-local.ps1 -UseMirrorOnly
```

### 可选：写 hosts 固化 IP（需管理员）

脚本用 `--resolve` 临时绑定 IP，不改系统；若要永久生效，可在 `C:\Windows\System32\drivers\etc\hosts` 追加（管理员权限，改前备份）：

```
20.247.166.64 github.com
185.199.111.133 release-assets.githubusercontent.com
```

IP 会随时间变化，失效后用 DoH 重新解析更新；代理软件（Clash/Mihomo）开启系统代理或 TUN 模式也可绕过 DNS 污染。

### git push 在本机被 DNS 卡住时

git 走 HTTPS 不支持 `--resolve`，可用「临时把 remote 切到可达 IP」的方式推送（推送后立即还原）：

```powershell
# 取凭据库里的 PAT（不要回显）
$input = "protocol=https`nhost=github.com`n`n"
$cred  = $input | git credential fill
$token = ($cred | Where-Object { $_ -like "password=*" }) -replace "password=", ""
$orig  = git remote get-url origin

# 切到可达 IP（IP 会漂移，先按上面 DoH 解析测通再用）
git remote set-url origin "https://Communist-t:$token@20.247.166.64/Communist-t/xhr-launcher.git"
git -c http.sslVerify=false -c http.extraHeader="Host: github.com" push origin main

# 还原标准 URL
git remote set-url origin $orig
```

> `http.sslVerify=false` 仅用于 IP 直连绕过证书主机名校验；正式环境走标准域名不要关闭校验。公共镜像（ghfast.top 等）只代理拉取，不代理带凭据的 push。

## 八、上线前必须处理的安全事项

1. **吊销已泄露的 Key**：本地历史里出现过的 `7361c7b2-...` 要到后端作废重发；新 Key 不进仓库，登录后由启动器回写 `user_config.json`。
2. **公开仓会暴露**后端域名、接口路径、WPF 源码与聊天 SOP 结构；`chat_strategy_config.json`、`my_contacts.json` 已在排除清单中，不得再提交。
3. 国内最终用户下载 200 MB+ 包，GitHub Release 直链较慢；正式分发建议把 `dist/*.zip` 再同步到对象存储/自有下载页，Release 仅作存档。

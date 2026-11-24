; xHR 启动器安装包（NSIS 3）
; 用法: makensis /DAppVer=1.0.4 /DPublishDir=D:\...\build\publish-1.0.4 xhr.nsi
; 产物: dist\xHR_<版本>_win-x64-setup.exe
;
; 形态与 setup.iss（已废弃的 Inno 方案）一致：
;   - per-user 安装（免 UAC），目录 %LOCALAPPDATA%\Programs\xHR
;   - 升级时直接强杀壳子及其子进程树（免等待）
;   - 只覆盖包内文件，不动用户生成的 config.json / 物料等
;   - 安装完成自动拉起新版（静默模式同样生效）

!include "MUI2.nsh"
!include "FileFunc.nsh"
!insertmacro GetTime

!ifndef AppVer
  !define AppVer "0.0.0"
!endif
!ifndef PublishDir
  !define PublishDir "..\build\publish-${AppVer}"
!endif
!ifndef VerNum
  !define VerNum "${AppVer}.0"
!endif
!ifndef OutName
  !define OutName "xHR_${AppVer}_win-x64-setup.exe"
!endif

!define MyAppName "xHR 启动器"
!define ExeName "xHR.exe"
!define UninstKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\xHR Launcher"

Name "${MyAppName}"
OutFile "..\dist\${OutName}"
InstallDir "$LOCALAPPDATA\Programs\xHR"
InstallDirRegKey HKCU "${UninstKey}" "InstallLocation"
RequestExecutionLevel user
Unicode true
SetCompressor /SOLID lzma

VIProductVersion "${VerNum}"
VIAddVersionKey "ProductName" "${MyAppName}"
VIAddVersionKey "ProductVersion" "${AppVer}"
VIAddVersionKey "FileVersion" "${AppVer}"
VIAddVersionKey "FileDescription" "${MyAppName} 安装程序"
VIAddVersionKey "LegalCopyright" "xHR"

; 安装包/卸载界面图标 = 程序本体 logo（与 csproj ApplicationIcon 同源）
!define MUI_ICON "${__FILEDIR__}\..\app.ico"
!define MUI_UNICON "${__FILEDIR__}\..\app.ico"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${ExeName}"

!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Var LGDay
Var LGMon
Var LGYear
Var LGDow
Var LGHou
Var LGMin
Var LGSec
Var LLogMsg
Var LLogHnd

; 追加一行日志到程序本体日志 %APPDATA%\xHR\launcher.log，
; 行格式与 C# 侧一致：[yyyy-MM-dd HH:mm:ss] [installer] 消息。
; 用法：Push "消息" 然后 Call LogLine。写入失败静默忽略，绝不影响安装。
Function LogLine
  Exch $LLogMsg
  ${GetTime} "" "L" $LGDay $LGMon $LGYear $LGDow $LGHou $LGMin $LGSec
  CreateDirectory "$APPDATA\xHR"
  ClearErrors
  FileOpen $LLogHnd "$APPDATA\xHR\launcher.log" a
  IfErrors lgdone
  FileSeek $LLogHnd 0 END
  FileWrite $LLogHnd "[$LGYear-$LGMon-$LGDay $LGHou:$LGMin:$LGSec] [installer] $LLogMsg$\r$\n"
  FileClose $LLogHnd
  lgdone:
  Pop $LLogMsg
FunctionEnd

Function .onInit
  Push "安装开始 v${AppVer}"
  Call LogLine
FunctionEnd

Section "Install"
  ; 更新场景：强杀壳子本体（不能用 /T——安装器自身也是壳子的子进程，会被连带杀掉）。
  ; 员工子进程挂在壳子的 JobObject 上（KILL_ON_JOB_CLOSE），壳子死亡时由系统连坐清理。
  ; 留 2 秒让文件句柄释放
  nsExec::Exec 'cmd /C taskkill /F /IM ${ExeName}'
  Pop $0
  Push "已终止运行中的 ${ExeName}"
  Call LogLine
  Sleep 2000
  CopyNow:
  SetOutPath "$INSTDIR"
  File /r "${PublishDir}\*.*"
  Push "文件复制完成 → $INSTDIR"
  Call LogLine
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${UninstKey}" "DisplayName" "${MyAppName}"
  WriteRegStr HKCU "${UninstKey}" "DisplayVersion" "${AppVer}"
  WriteRegStr HKCU "${UninstKey}" "Publisher" "xHR"
  WriteRegStr HKCU "${UninstKey}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UninstKey}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "${UninstKey}" "NoModify" 1
  WriteRegDWORD HKCU "${UninstKey}" "NoRepair" 1
  Push "安装完成 v${AppVer}"
  Call LogLine
SectionEnd

Section "快捷方式"
  CreateDirectory "$SMPROGRAMS\xHR"
  CreateShortCut "$SMPROGRAMS\xHR\xHR 启动器.lnk" "$INSTDIR\${ExeName}"
  CreateShortCut "$DESKTOP\xHR.lnk" "$INSTDIR\${ExeName}"
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\xHR.lnk"
  Delete "$SMPROGRAMS\xHR\xHR 启动器.lnk"
  RMDir "$SMPROGRAMS\xHR"
  ; NSIS 卸载器自动删除安装时记录的文件；用户运行期生成的 config.json 等保留
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UninstKey}"
SectionEnd

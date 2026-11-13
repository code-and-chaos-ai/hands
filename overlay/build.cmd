@echo off
rem Builds overlay.exe with the .NET Framework compiler that ships with Windows (no installs).
"%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /optimize+ /target:winexe /platform:x64 ^
  /out:"%~dp0overlay.exe" /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll ^
  "%~dp0Overlay.cs"

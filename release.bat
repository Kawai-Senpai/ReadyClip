@echo off
rem Tag and push a release. GitHub Actions then builds the exe zip.
rem   release.bat            asks for the version
rem   release.bat 1.0.1      no questions except the final confirm
setlocal EnableDelayedExpansion
cd /d "%~dp0"

git rev-parse --is-inside-work-tree >nul 2>&1 || (echo This folder is not a git repository. & goto :fail)
git remote get-url origin >nul 2>&1 || (echo No "origin" remote. Add one with: git remote add origin URL & goto :fail)

git status --porcelain | findstr . >nul && (
    echo You have uncommitted changes. Commit or stash them first:
    git status --short
    goto :fail
)

set "LAST="
for /f "delims=" %%t in ('git describe --tags --abbrev^=0 2^>nul') do set "LAST=%%t"
if defined LAST (echo Last release: !LAST!) else (echo No releases yet.)

set "VER=%~1"
if "!VER!"=="" set /p "VER=New version (e.g. 1.0.1): "
if "!VER!"=="" (echo No version given. & goto :fail)
if /i "!VER:~0,1!"=="v" set "VER=!VER:~1!"
echo(!VER!| findstr /r "^[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*$" >nul || (echo "!VER!" is not a version like 1.2.3 & goto :fail)
set "TAG=v!VER!"

git rev-parse -q --verify "refs/tags/!TAG!" >nul && (echo Tag !TAG! already exists locally. & goto :fail)
git ls-remote --exit-code --tags origin "refs/tags/!TAG!" >nul 2>&1 && (echo Tag !TAG! already exists on GitHub. & goto :fail)

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%b"
echo.
echo About to release !TAG! from branch !BRANCH!:
echo   - set __version__ = "!VER!" and commit
echo   - create tag !TAG!
echo   - push !BRANCH! and !TAG! to origin (starts the GitHub build)
set "OK="
set /p "OK=Continue? [y/N] "
if /i not "!OK!"=="y" (echo Cancelled. & goto :fail)

rem No double quotes inside the PowerShell code: they break cmd's quote parsing.
powershell -NoProfile -Command "$p = Join-Path (Get-Location) 'readyclip\__init__.py'; $q = [char]34; $new = '__version__ = ' + $q + $env:VER + $q; $t = [IO.File]::ReadAllText($p) -replace ('__version__ = ' + $q + '[^' + $q + ']*' + $q), $new; if (-not $t.Contains($new)) { exit 1 }; [IO.File]::WriteAllText($p, $t)"
if errorlevel 1 (echo Could not update __version__ in readyclip\__init__.py & goto :fail)
git diff --quiet -- readyclip/__init__.py || (
    git add readyclip/__init__.py || goto :fail
    git commit -q -m "Release !TAG!" || goto :fail
)
git tag -a "!TAG!" -m "ReadyClip !TAG!" || goto :fail
git push origin "!BRANCH!" || goto :fail
git push origin "!TAG!" || goto :fail

echo.
echo Pushed !TAG!. The build takes about 5 minutes; watch it on the repo's Actions tab.
echo The zip will appear on the Releases page when it finishes.
if "%~1"=="" pause
exit /b 0

:fail
if "%~1"=="" pause
exit /b 1

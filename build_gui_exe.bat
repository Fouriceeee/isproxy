@echo off
setlocal
cd /d "%~dp0"

rem Read version from pyproject.toml and append it to the exe name
for /f "tokens=2 delims==" %%v in ('findstr /b /c:"version = " pyproject.toml') do set "VERSION=%%v"
set "VERSION=%VERSION: =%"
set "VERSION=%VERSION:"=%"
set "VERSION=%VERSION:'=%"

if not defined VERSION (
  echo Could not read version from pyproject.toml.
  exit /b 1
)

set "NAME=isproxy-gui-%VERSION%"

echo Building %NAME%.exe ...
uv run --with "pyinstaller>=6.22" pyinstaller ^
  --noconfirm --clean --onefile --windowed ^
  --name %NAME% ^
  --paths src ^
  --specpath build --workpath build --distpath dist ^
  launcher_gui.py

if errorlevel 1 (
  echo.
  echo Build failed.
  exit /b 1
)

echo.
echo Built: %CD%\dist\%NAME%.exe

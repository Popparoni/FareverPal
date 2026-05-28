@echo off
setlocal
cd /d %~dp0

echo [1/2] Building Rust memory reader (farever_native)...
set PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
.venv\Scripts\python.exe -m maturin develop --release -m native\Cargo.toml
if errorlevel 1 goto err

echo [2/2] Import sanity check...
.venv\Scripts\python.exe -c "import farever_native; print('farever_native', farever_native.__version__)"
if errorlevel 1 goto err

echo.
echo OK. Run with:  .venv\Scripts\python.exe -m farever_companion
goto :eof

:err
echo.
echo BUILD FAILED
exit /b 1

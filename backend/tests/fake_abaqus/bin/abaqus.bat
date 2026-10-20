@echo off
rem Fake Abaqus launcher for tests on Windows; same behaviour as the shell script "abaqus" next to it:
rem   abaqus python script.py      -> run the script with the fake odbAccess
rem   abaqus cae script=boot.py    -> a simulated CAE session (fake_cae.py)
rem   abaqus job=X ... / terminate -> a simulated analysis run (fake_job.py)
rem cmd splits arguments at "=", so fake_cae.py also accepts "script boot.py".
setlocal
set "HERE=%~dp0.."
set "PY=%NOCODE_TEST_PYTHON%"
if not defined PY set "PY=python"
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
echo Abaqus License Manager checked out the following license(s): (fake banner line)
set "MODE=%~1"
shift
set ARGS=
:collect
if "%~1"=="" goto run
set ARGS=%ARGS% "%~1"
shift
goto collect
:run
set GUI=
if /i "%MODE%"=="cae" set GUI=1
if /i "%MODE%"=="viewer" set GUI=1
set JOB=
if /i "%MODE:~0,3%"=="job" set JOB=1
if /i "%MODE%"=="terminate" set JOB=1
if defined JOB (
  "%PY%" "%HERE%\fake_job.py" "%MODE%" %ARGS%
  exit /b %ERRORLEVEL%
)
if defined GUI (
  "%PY%" "%HERE%\fake_cae.py" %ARGS%
) else (
  "%PY%" %ARGS%
)
exit /b %ERRORLEVEL%

@echo off
rem MedVerify camera set-up, guided: run whenever the camera or the sheet has moved.
rem Needs the server and camera running (live_demo.bat, or  mv up). Tape the sheet down first.
rem The previous calibration is copied to data\calibration_backup\ before anything changes.
cd /d "%~dp0"
setlocal enabledelayedexpansion

for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set TS=%%t
set BK=data\calibration_backup\%TS%
mkdir "%BK%" >nul 2>&1
for %%f in (config\rois.json data\vision_baseline.json data\vision_baseline.npz data\vision_empty_frame.png) do (
    if exist %%f copy /y %%f "%BK%\" >nul
)
echo Old calibration saved in %BK%
echo.

echo == 1/3  Finding the drawn squares (takes one picture) ==
call mv cam regions preview
echo Must list 4 squares: 1=A top-left, 2=B top-right, 3=C bottom-left, 4=D bottom-right.
choice /m "Use these squares"
if errorlevel 2 (echo Darken faint lines or reframe the camera, then run this again. & exit /b 1)
call mv cam regions auto
echo.

echo == 2/3  EMPTY photo ==
echo Take ALL pieces off the sheet, keep hands out of view.
pause
call mv cam empty
echo.

echo == 3/3  LOADED photo ==
echo Put the pieces back, each fully inside its square, then hands away.
set /p A=How many in A?
set /p B=How many in B?
set /p C=How many in C?
set /p D=How many in D?
call mv cam loaded A=!A! B=!B! C=!C! D=!D!
echo.
call mv status
echo Set-up done. If something went wrong:  restore_camera.bat %TS%

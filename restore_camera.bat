@echo off
rem Put back a calibration saved by setup_camera.bat:  restore_camera.bat 20261102-233000
rem Without a name: lists the saved ones. Restarts the camera service to load it.
cd /d "%~dp0"
if "%1"=="" (echo Saved calibrations: & dir /b data\calibration_backup & exit /b 0)
set BK=data\calibration_backup\%1
if not exist "%BK%\rois.json" (echo No calibration %BK% & exit /b 1)
copy /y "%BK%\rois.json" config\ >nul
for %%f in (vision_baseline.json vision_baseline.npz vision_empty_frame.png) do copy /y "%BK%\%%f" data\ >nul
echo Restored %BK%. Restarting...
call mv down
call mv up

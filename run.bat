@echo off
rem Double-click to open Video Merger in your browser, or drag folders or video files onto this file:
rem the page opens with them as places to look in. (In a terminal: python -m video_merger --help)
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "VIDEO_MERGER_LAUNCHER=1"
where py >NUL 2>NUL || goto python
py -3 -m video_merger %*
exit /b %errorlevel%
:python
python -m video_merger %*

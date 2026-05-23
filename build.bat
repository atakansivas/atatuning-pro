@echo off
echo Installing dependencies...
pip install pywebview pyinstaller bleak
echo Building AtaTuning Pro EXE with Native Bluetooth...
pyinstaller --noconsole --onefile --add-data "index.html;." --collect-all bleak --name "AtaTuningPro" app.py
echo Done! Check the 'dist' folder.
pause

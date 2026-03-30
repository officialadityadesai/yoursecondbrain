@echo off
echo Installing My Second Brain dependencies...
echo.
echo Installing Python backend dependencies...
cd backend
pip install -r requirements.txt
cd ..
echo.
echo Installing Node.js frontend dependencies...
cd frontend
npm install
npm run build
cd ..
echo.
if not exist scripts mkdir scripts
echo Creating startup helper scripts...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create-startup-task.ps1" -CreateOnly
echo Setup Complete!
echo 1. Rename .env.example to .env
echo 2. Add your FREE Gemini API Key
echo 3. Double-click run.bat to start My Second Brain.
echo 4. Optional: Run scripts\create-startup-task.ps1 to auto-start on Windows login.
echo 5. If you change frontend code later, rebuild once with: cd frontend ^&^& npm run build
echo 6. Optional: Run scripts\setup_mcp.bat to connect My Second Brain to Claude Desktop.
pause

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
VK Suggester - Launcher script
Alternative launcher that doesn't depend on batch file encoding
"""
import os
import sys
import subprocess
import time
import webbrowser
from pathlib import Path

def check_python():
    """Check Python version"""
    if sys.version_info < (3, 8):
        print("[ERROR] Python 3.8+ required!")
        print(f"Current version: {sys.version}")
        input("Press Enter to exit...")
        sys.exit(1)
    print("[1/4] Python check... OK")
    print()

def setup_venv():
    """Setup virtual environment"""
    venv_path = Path("venv")
    
    if not venv_path.exists():
        print("[2/4] Creating virtual environment...")
        result = subprocess.run([sys.executable, "-m", "venv", "venv"], 
                              capture_output=True)
        if result.returncode != 0:
            print("[ERROR] Failed to create virtual environment!")
            print(result.stderr.decode('utf-8', errors='ignore'))
            input("Press Enter to exit...")
            sys.exit(1)
        print("Virtual environment created!")
    else:
        print("[2/4] Virtual environment... OK")
    print()

def activate_venv():
    """Activate virtual environment"""
    if sys.platform == "win32":
        activate_script = Path("venv/Scripts/activate.bat")
        python_exe = Path("venv/Scripts/python.exe")
    else:
        activate_script = Path("venv/bin/activate")
        python_exe = Path("venv/bin/python")
    
    if not python_exe.exists():
        print("[ERROR] Virtual environment Python not found!")
        input("Press Enter to exit...")
        sys.exit(1)
    
    return python_exe

def install_dependencies(python_exe):
    """Install dependencies"""
    print("[3/4] Installing dependencies...")
    requirements = Path("requirements.txt")
    
    if not requirements.exists():
        print("[WARNING] requirements.txt not found!")
        print()
        return
    
    result = subprocess.run([str(python_exe), "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                          capture_output=True)
    if result.returncode != 0:
        print("[ERROR] Failed to install dependencies!")
        print(result.stderr.decode('utf-8', errors='ignore'))
        input("Press Enter to exit...")
        sys.exit(1)
    print("Dependencies installed!")
    print()

def start_app(python_exe):
    """Start the application"""
    print("[4/4] Starting application...")
    print()
    print("=" * 40)
    print("    Application starting...")
    print("    Browser will open in 3 seconds")
    print("=" * 40)
    print()
    print("URL: http://localhost:5000")
    print()
    print("Press Ctrl+C to stop")
    print()
    
    # Open browser after 3 seconds
    def open_browser():
        time.sleep(3)
        webbrowser.open("http://localhost:5000")
    
    import threading
    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()
    
    # Run the application
    app_path = Path("app.py")
    if not app_path.exists():
        print("[ERROR] app.py not found!")
        input("Press Enter to exit...")
        sys.exit(1)
    
    try:
        subprocess.run([str(python_exe), "app.py"])
    except KeyboardInterrupt:
        print("\n[INFO] Application stopped by user")
    except Exception as e:
        print(f"[ERROR] {e}")
        input("Press Enter to exit...")
        sys.exit(1)
    
    print()
    print("=" * 40)
    print("Application stopped")
    print("=" * 40)
    input("Press Enter to exit...")

def main():
    """Main launcher function"""
    os.chdir(Path(__file__).parent)
    
    print("=" * 40)
    print("    VK Suggester - Launcher")
    print("=" * 40)
    print()
    
    check_python()
    setup_venv()
    python_exe = activate_venv()
    install_dependencies(python_exe)
    start_app(python_exe)

if __name__ == "__main__":
    main()

import os
import sys
import subprocess
import shutil

# To build: python build_exe.py

def install_and_import(package):
    try:
        __import__(package)
    except ImportError:
        print(f"{package} not found. Installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package])

# Ensure PyInstaller is installed
install_and_import('PyInstaller')
import PyInstaller.__main__

def build():
    print("Ensuring no instances of SystemIdleHelper are running...")
    # Try to terminate any running instances of the target executable
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/IM", "SystemIdleHelper.exe", "/T"], 
                           capture_output=True, check=False)
    except Exception:
        pass

    print("Building SystemIdleHelper.exe...")
    PyInstaller.__main__.run([
        'system_idle_helper.py',
        '--onefile',
        '--noconsole',
        '--name=SystemIdleHelper',
        '--clean',
    ])

if __name__ == "__main__":
    build()

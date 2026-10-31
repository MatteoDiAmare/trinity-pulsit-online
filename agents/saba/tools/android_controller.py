import os
import subprocess
from pathlib import Path

ADB = os.environ.get(
    "SABA_ADB",
    "/mnt/c/Users/Matteo/AppData/Local/Android/Sdk/platform-tools/adb.exe",
)

class AndroidController:
    def run(self, *args):
        return subprocess.run(
            [ADB, *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def open_settings(self):
        return self.run("shell", "am", "start", "-a", "android.settings.SETTINGS")

    def screenshot(self, destination):
        destination = Path(destination).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)

        self.run("shell", "screencap", "-p", "/sdcard/saba-screen.png")
        windows_path = subprocess.run(
            ["wslpath", "-w", str(destination)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.run("pull", "/sdcard/saba-screen.png", windows_path)
        return destination

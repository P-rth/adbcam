# AdbCam

A script to use your Android phone as a virtual camera and microphone on your Linux PC using `scrcpy`, `v4l2loopback`, and `PulseAudio`.

---

## 📦 Requirements

| Program        | Description                          |
|-------------|--------------------------------------|
| `python >= 3.7` |       Python to run the program              |
| `adb`       | Android Debug Bridge                 |
| `scrcpy`    | Stream Android screen/camera         |
| `v4l2loopback` | Creates virtual video device     |
| `pactl`     | PulseAudio control utility           |

---

## 💻 Installation

### On Arch Linux (and derivatives)

```bash
sudo pacman -Syu scrcpy android-tools v4l2loopback-dkms pulseaudio
````

---

### On Ubuntu / Debian

```bash
sudo apt update
sudo apt install scrcpy adb v4l2loopback-dkms pulseaudio-utils
```

---

## ▶️ Run

```bash
chmod +x adbcam.py
./adbcam.py
```

Follow the prompts to select camera, resolution, FPS, and mic source.

---

## Stop

Press `Ctrl + C` to stop the script and clean up resources.

---

## Output

* **Video**: Virtual webcam at `/dev/video0`
* **Audio**: Virtual mic as `"AdbCam"` in audio apps

Use them in OBS, Zoom, Discord, etc. (all apps are supported)

---
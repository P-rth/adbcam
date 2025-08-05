#!/usr/bin/env python3

import os
import sys
import signal
import subprocess
import time
import atexit
import re
import threading
import queue
from typing import List, Dict, Tuple, Optional

# ========== CONFIGURATION ==========
V4L2_DEVICE = "/dev/video0"
CARD_LABEL = "AdbCam"
DEFAULT_FPS = "60"

# Audio configuration
VIRTUAL_MIC_SOURCE = "AdbCam"
PIPE_PATH = "/tmp/adbcam_pipe"

# Available microphone sources
MIC_SOURCES = {
    "1": ("mic", "Standard microphone"),
    "2": ("mic-unprocessed", "Unprocessed (raw) microphone"),
    "3": ("mic-camcorder", "Microphone tuned for video recording"),
    "4": ("mic-voice-recognition", "Microphone tuned for voice recognition"),
    "5": ("mic-voice-communication", "Microphone tuned for voice communications (voice calls)")
}

# Global variables for cleanup
MODULE_ID = None
scrcpy_processes = []
monitoring_threads = []
device_disconnected = threading.Event()

# CLEANUP
def cleanup():
    print("[*] Cleaning up...")
    
    # Stop monitoring threads
    for thread in monitoring_threads:
        if thread.is_alive():
            thread.daemon = True
    
    # Unload PulseAudio module
    if MODULE_ID:
        try:
            subprocess.run(["pactl", "unload-module", MODULE_ID], check=False, 
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[!] Error unloading module: {e}")
    
    # Remove pipe
    if os.path.exists(PIPE_PATH):
        try:
            os.remove(PIPE_PATH)
        except Exception as e:
            print(f"[!] Error removing pipe: {e}")
    
    # Kill scrcpy processes
    try:
        subprocess.run(["pkill", "-f", "scrcpy"], check=False,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[!] Error killing scrcpy processes: {e}")
    
    # Terminate any tracked processes
    for proc in scrcpy_processes:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except:
            try:
                proc.kill()
            except:
                pass

def signal_handler(signum, frame):
    cleanup()
    sys.exit(0)

# Register cleanup function and signal handlers for a clean ctrl c 
atexit.register(cleanup)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def check_adb_devices() -> bool:
    """Check if any ADB devices are connected"""
    print("[*] Checking for connected ADB devices...")
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10
        )
        
        
        lines = result.stdout.strip().split('\n')
        devices = []
        
        for line in lines[1:]:  
            line = line.strip()
            if line and not line.startswith('*'):
                parts = line.split('\t')
                if len(parts) >= 2 and parts[1] == 'device':
                    devices.append(parts[0])
        
        if devices:
            print(f"[+] Found ADB device(s): {', '.join(devices)}")
            return True
        else:
            print("[!] No ADB devices found in 'device' state")
            return False
            
    except subprocess.TimeoutExpired:
        print("[!] Timeout checking ADB devices")
        return False
    except subprocess.CalledProcessError as e:
        print(f"[!] Failed to check ADB devices: {e}")
        if e.stderr:
            print(f"[!] ADB Error: {e.stderr}")
        return False
    except FileNotFoundError:
        print("[!] ADB command not found. Please install Android Debug Bridge (adb)")
        return False
    except Exception as e:
        print(f"[!] Error checking ADB devices: {e}")
        return False

def monitor_process_output(proc, process_name):
    """Monitor process output for errors and disconnection warnings"""
    def read_stream(stream, stream_name):
        try:
            for line in iter(stream.readline, ''): 
                if not line:
                    break
                
                line_str = line.strip()
                if not line_str:
                    continue
                
                # Check for device disconnection
                if "Device disconnected" in line_str and "WARN:" in line_str:
                    print(f"[!] {process_name}: Device disconnected detected!")
                    device_disconnected.set()
                    return
                
                # Check for ADB device error
                if "Could not find any ADB device" in line_str:
                    print(f"[!] {process_name}: No ADB device found!")
                    device_disconnected.set()
                    return
                
                # Show errors and important warnings
                if any(keyword in line_str for keyword in ["ERROR:", "FATAL:", "Failed", "Error", "Cannot"]):
                    print(f"[!] {process_name} ({stream_name}): {line_str}")
                elif "WARN:" in line_str and not "Device disconnected" in line_str:
                    # Show other warnings but not device disconnected
                    print(f"[W] {process_name} ({stream_name}): {line_str}")
        except Exception as e:
            print(f"[!] Error monitoring {process_name} {stream_name}: {e}")
    
    # Create threads for stdout and stderr
    if proc.stdout:
        stdout_thread = threading.Thread(target=read_stream, args=(proc.stdout, "stdout"), daemon=True)
        stdout_thread.start()
    
    if proc.stderr:
        stderr_thread = threading.Thread(target=read_stream, args=(proc.stderr, "stderr"), daemon=True)
        stderr_thread.start()

def run_command(cmd, capture_output=False):
    """Run a shell command and return the result"""
    try:
        if capture_output:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        else:
            subprocess.run(cmd, shell=True, check=True, 
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except subprocess.CalledProcessError as e:
        print(f"[!] Command failed: {cmd}")
        print(f"[!] Error: {e}")
        return False

def parse_camera_info(output: str) -> Dict[str, Dict]:
    """Parse the camera information from scrcpy --list-camera-sizes output"""
    cameras = {}
    current_camera = None
    
    lines = output.split('\n')
    for line in lines:
        line = line.strip()
        
        # Look for camera ID lines
        camera_match = re.match(r'--camera-id=(\d+)\s+\(([^,]+),\s*(\d+x\d+),\s*fps=\[([^\]]+)\]\)', line)
        if camera_match:
            camera_id = camera_match.group(1)
            camera_type = camera_match.group(2)
            default_res = camera_match.group(3)
            fps_range = camera_match.group(4)
            
            cameras[camera_id] = {
                'type': camera_type,
                'default_resolution': default_res,
                'fps_range': fps_range,
                'resolutions': []
            }
            current_camera = camera_id
        
        # Look for resolution lines
        elif current_camera and re.match(r'^\s*-\s*\d+x\d+\s*$', line):
            resolution = line.strip('- ').strip()
            cameras[current_camera]['resolutions'].append(resolution)
    
    return cameras

def get_camera_info() -> Optional[Dict[str, Dict]]:
    """Get camera information from scrcpy"""
    print("[*] Getting camera information...")
    try:
        result = subprocess.run(
            ["scrcpy", "--list-camera-sizes"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30
        )
        
        # Check for ADB device error in the output
        if "Could not find any ADB device" in result.stderr:
            print("[!] Error output: ERROR: Could not find any ADB device")
            return None
        
        return parse_camera_info(result.stdout)
    except subprocess.TimeoutExpired:
        print("[!] Timeout getting camera information")
        return None
    except subprocess.CalledProcessError as e:
        print(f"[!] Failed to get camera information: {e}")
        if e.stderr:
            print(f"[!] Error output: {e.stderr}")
            # Check for ADB device error in stderr
            if "Could not find any ADB device" in e.stderr:
                print("[!] No ADB device found - cannot proceed")
                return None
        return None
    except Exception as e:
        print(f"[!] Error getting camera information: {e}")
        return None

def select_camera(cameras: Dict[str, Dict]) -> Tuple[str, str, str]:
    """Let user select camera, resolution, and FPS"""
    if not cameras:
        print("[!] No cameras found")
        return "0", "1920x1080", DEFAULT_FPS
    
    print("\n[*] Available cameras:")
    for camera_id, info in cameras.items():
        print(f"  {camera_id}: {info['type']} camera (default: {info['default_resolution']}, fps: [{info['fps_range']}])")
    
    # Select camera
    while True:
        try:
            selected_camera = input(f"\nSelect camera ID (default: 0): ").strip()
            if not selected_camera:
                selected_camera = "0"
            
            if selected_camera in cameras:
                break
            else:
                print(f"[!] Invalid camera ID. Available: {', '.join(cameras.keys())}")
        except KeyboardInterrupt:
            print("\n[*] Cancelled by user")
            sys.exit(0)
    
    camera_info = cameras[selected_camera]
    resolutions = camera_info['resolutions']
    
    print(f"\n[*] Available resolutions for camera {selected_camera} ({camera_info['type']}):")
    
    # easy selection for common res
    common_resolutions = ['1920x1080', '1280x720', '640x480', '1920x1440', '2560x1440', '3840x2160']
    other_resolutions = [r for r in resolutions if r not in common_resolutions]
    
    print("  Common resolutions:")
    for i, res in enumerate(common_resolutions):
        if res in resolutions:
            print(f"    {i+1}: {res}")
    
    if other_resolutions:
        print("  Other resolutions:")
        start_idx = len([r for r in common_resolutions if r in resolutions]) + 1
        for i, res in enumerate(other_resolutions):
            print(f"    {start_idx + i}: {res}")
    
    # Select resolution
    all_available = [r for r in common_resolutions if r in resolutions] + other_resolutions
    
    while True:
        try:
            res_input = input(f"\nSelect resolution (1-{len(all_available)}, default: 1920x1080): ").strip()
            
            if not res_input:
                selected_resolution = "1920x1080" if "1920x1080" in resolutions else resolutions[0]
                break
            
            try:
                res_idx = int(res_input) - 1
                if 0 <= res_idx < len(all_available):
                    selected_resolution = all_available[res_idx]
                    break
                else:
                    print(f"[!] Invalid selection. Choose 1-{len(all_available)}")
            except ValueError:
                print("[!] Please enter a number")
        except KeyboardInterrupt:
            print("\n[*] Cancelled by user")
            sys.exit(0)
    
    # Select FPS
    fps_options = [int(x.strip()) for x in camera_info['fps_range'].split(',')]
    print(f"\n[*] Available FPS rates: {fps_options}")
    
    while True:
        try:
            fps_input = input(f"Select FPS ({'/'.join(map(str, fps_options))}, default: {max(fps_options)}): ").strip()
            
            if not fps_input:
                selected_fps = str(max(fps_options))
                break
            
            try:
                fps_val = int(fps_input)
                if fps_val in fps_options:
                    selected_fps = fps_input
                    break
                else:
                    print(f"[!] Invalid FPS. Available: {fps_options}")
            except ValueError:
                print("[!] Please enter a valid number")
        except KeyboardInterrupt:
            print("\n[*] Cancelled by user")
            sys.exit(0)
    
    return selected_camera, selected_resolution, selected_fps

def select_microphone_source() -> str:
    """Let user select microphone source"""
    print("\n[*] Available microphone sources:")
    for key, (source, description) in MIC_SOURCES.items():
        print(f"  {key}: {source} - {description}")
    
    while True:
        try:
            mic_input = input(f"\nSelect microphone source (1-{len(MIC_SOURCES)}, default: 3): ").strip()
            
            if not mic_input:
                return MIC_SOURCES["3"][0]  # Default to mic-camcorder
            
            if mic_input in MIC_SOURCES:
                return MIC_SOURCES[mic_input][0]
            else:
                print(f"[!] Invalid selection. Choose 1-{len(MIC_SOURCES)}")
        except KeyboardInterrupt:
            print("\n[*] Cancelled by user")
            sys.exit(0)

def check_v4l2loopback():
    """Check if v4l2loopback module is loaded"""
    try:
        result = subprocess.run(["lsmod"], capture_output=True, text=True, check=True)
        return "v4l2loopback" in result.stdout
    except subprocess.CalledProcessError:
        return False

def load_v4l2loopback():
    """Load the v4l2loopback module"""
    if not check_v4l2loopback():
        print("[+] Loading v4l2loopback module...")
        cmd = f'sudo modprobe v4l2loopback devices=1 video_nr=0 card_label="{CARD_LABEL}" exclusive_caps=1'
        if not run_command(cmd):
            print("[!] Failed to load v4l2loopback module")
            return False
    else:
        print("[i] v4l2loopback already loaded.")
    return True

def setup_virtual_mic():
    """Setup PulseAudio virtual microphone"""
    global MODULE_ID
    
    print(f"[+] Setting up PulseAudio virtual mic: {VIRTUAL_MIC_SOURCE}")
    
    # Remove existing pipe if it exists
    if os.path.exists(PIPE_PATH):
        os.remove(PIPE_PATH)
    
    # Create named pipe
    try:
        os.mkfifo(PIPE_PATH)
    except OSError as e:
        print(f"[!] Failed to create pipe: {e}")
        return False
    
    # Load PulseAudio module
    cmd = f'pactl load-module module-pipe-source source_name="{VIRTUAL_MIC_SOURCE}" channels=2 format=s16le rate=48000 file="{PIPE_PATH}"'
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
        MODULE_ID = result.stdout.strip()
        return True
    except subprocess.CalledProcessError as e:
        print(f"[!] Failed to load PulseAudio module: {e}")
        return False

def start_scrcpy_video(camera_id: str, resolution: str, fps: str):
    """Start scrcpy for video capture"""
    print(f"[+] Starting scrcpy (video) -> {V4L2_DEVICE}")
    print(f"    Camera: {camera_id}, Resolution: {resolution}, FPS: {fps}")
    
    cmd = [
        "scrcpy",
        "--video-source=camera",
        f"--camera-id={camera_id}",
        "--no-audio",
        f"--v4l2-sink={V4L2_DEVICE}",
        f"--camera-size={resolution}",
        f"--camera-fps={fps}",
        "--port", "27183",
        "--no-window"
    ]
    
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,  # This ensures strings instead of bytes
            bufsize=1
        )
        scrcpy_processes.append(proc)
        
        # Start monitoring thread
        monitor_thread = threading.Thread(
            target=monitor_process_output,
            args=(proc, "Video"),
            daemon=True
        )
        monitor_thread.start()
        monitoring_threads.append(monitor_thread)
        
        return proc
    except Exception as e:
        print(f"[!] Failed to start scrcpy video: {e}")
        return None

def start_scrcpy_audio(mic_source: str):
    """Start scrcpy for audio capture"""
    print(f"[+] Starting scrcpy (audio) -> {VIRTUAL_MIC_SOURCE}")
    print(f"    Microphone source: {mic_source}")
    
    cmd = [
        "scrcpy",
        "--no-video",
        "--no-playback",
        f"--audio-source={mic_source}",
        "--audio-codec=raw",
        "--no-window",
        f"--record={PIPE_PATH}",
        "--port", "27184",
        "--record-format=wav"
    ]
    
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,  # This ensures strings instead of bytes
            bufsize=1
        )
        scrcpy_processes.append(proc)
        
        # Start monitoring thread
        monitor_thread = threading.Thread(
            target=monitor_process_output,
            args=(proc, "Audio"),
            daemon=True
        )
        monitor_thread.start()
        monitoring_threads.append(monitor_thread)
        
        return proc
    except Exception as e:
        print(f"[!] Failed to start scrcpy audio: {e}")
        return None

def main():
    """Main function"""
    print("[*] AdbCam Setup - Enhanced Version")
    print("[*] =====================================")
    
    # First check for ADB devices
    if not check_adb_devices():
        print("\n[!] SETUP FAILED: No ADB devices found")
        print("[*] Please ensure:")
        print("    1. Your Android device is connected via USB")
        print("    2. USB debugging is enabled on your device")
        print("    3. You have authorized this computer on your device")
        print("    4. ADB is properly installed")
        print("\n[*] Try running 'adb devices' manually to troubleshoot")
        sys.exit(1)
    
    # Get camera information
    cameras = get_camera_info()
    if cameras is None:
        print("\n[!] SETUP FAILED: Could not get camera information")
        print("[*] This usually means:")
        print("    1. No ADB device is connected")
        print("    2. The device doesn't support camera access via scrcpy")
        print("    3. Camera permissions are not granted")
        sys.exit(1)
    
    if not cameras:
        print("[!] No cameras found on the device")
        camera_id, resolution, fps = "0", "1920x1080", DEFAULT_FPS
    else:
        camera_id, resolution, fps = select_camera(cameras)
    
    # Select microphone source
    mic_source = select_microphone_source()
    
    print(f"\n[*] Configuration selected:")
    print(f"    Camera ID: {camera_id}")
    print(f"    Resolution: {resolution}")
    print(f"    FPS: {fps}")
    print(f"    Microphone: {mic_source}")
    print(f"    V4L2 Device: {V4L2_DEVICE}")
    
    input("\nPress Enter to continue with setup...")
    
    # Check and load v4l2loopback
    if not load_v4l2loopback():
        print("[!] Failed to setup v4l2loopback")
        sys.exit(1)
    
    # Setup virtual microphone
    if not setup_virtual_mic():
        print("[!] Failed to setup virtual microphone")
        sys.exit(1)
    
    # Start scrcpy instances
    video_proc = start_scrcpy_video(camera_id, resolution, fps)
    if not video_proc:
        print("[!] Failed to start video capture")
        sys.exit(1)
    
    # Wait a moment before starting audio to avoid port conflicts
    time.sleep(2)
    
    audio_proc = start_scrcpy_audio(mic_source)
    if not audio_proc:
        print("[!] Failed to start audio capture")
        sys.exit(1)
    
    # Print success message
    print()
    print("=" * 60)
    print("[*] Setup complete!")
    print(f"[*] Camera is available at {V4L2_DEVICE} (select '{CARD_LABEL}' in video apps)")
    print(f"[*] Android mic is available as '{VIRTUAL_MIC_SOURCE}' (select as microphone in apps)")
    print("[*] Press Ctrl+C to stop and clean up")
    print("[*] Monitoring for device disconnection...")
    print("=" * 60)
    print()
    
    # Wait for processes, user interrupt, or device disconnection
    try:
        while True:
            #device disconnection
            if device_disconnected.is_set():
                print("[!] Device disconnection detected - stopping...")
                break
            
            #process died
            for proc in scrcpy_processes[:]:  # Create a copy of the list
                if proc.poll() is not None:
                    print(f"[!] A scrcpy process has terminated unexpectedly (exit code: {proc.returncode})")
                    scrcpy_processes.remove(proc)
            
            if not scrcpy_processes:
                print("[!] All scrcpy processes have terminated")
                break
                
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\n[*] Interrupted by user")
    
    cleanup()

if __name__ == "__main__":
    main()
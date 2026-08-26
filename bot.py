import asyncio
import ast
import ctypes
import difflib
import json
import logging
import math
import operator
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import winsound
from ctypes import wintypes
from datetime import datetime, timedelta

import psutil
from dotenv import load_dotenv
from telegram import InputMediaPhoto, Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log"),
)
log = logging.getLogger("telegram-remote")


def open_chrome():
    subprocess.Popen("start chrome", shell=True)
    return "Opening Chrome."


def open_notepad():
    subprocess.Popen(["notepad.exe"])
    return "Opening Notepad."


def open_explorer():
    subprocess.Popen(["explorer.exe"])
    return "Opening File Explorer."


def open_downloads():
    subprocess.Popen(["explorer.exe", os.path.expanduser("~\\Downloads")])
    return "Opening Downloads."


def open_desktop():
    subprocess.Popen(["explorer.exe", os.path.expanduser("~\\Desktop")])
    return "Opening Desktop."


def open_vscode():
    subprocess.Popen("code .", shell=True, cwd=os.path.expanduser("~"))
    return "Opening VS Code."


def lock_pc():
    import ctypes
    ctypes.windll.user32.LockWorkStation()
    return "Locked."


# 60s delay so a shutdown/restart can be cancelled with "cancel shutdown"
# if it was triggered by mistake.
SHUTDOWN_DELAY_SECONDS = 60


def shutdown_pc():
    subprocess.run(["shutdown", "/s", "/t", str(SHUTDOWN_DELAY_SECONDS)])
    return f"Shutting down in {SHUTDOWN_DELAY_SECONDS}s. Send 'cancel shutdown' to abort."


def restart_pc():
    subprocess.run(["shutdown", "/r", "/t", str(SHUTDOWN_DELAY_SECONDS)])
    return f"Restarting in {SHUTDOWN_DELAY_SECONDS}s. Send 'cancel shutdown' to abort."


def cancel_shutdown():
    subprocess.run(["shutdown", "/a"])
    return "Pending shutdown/restart cancelled."


def sleep_pc():
    subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
    return "Sleeping."


def _volume_interface():
    from pycaw.utils import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


def mute():
    _volume_interface().SetMute(1, None)
    return "Muted."


def unmute():
    _volume_interface().SetMute(0, None)
    return "Unmuted."


def volume_up():
    vol = _volume_interface()
    level = max(0.0, min(1.0, vol.GetMasterVolumeLevelScalar() + 0.1))
    vol.SetMasterVolumeLevelScalar(level, None)
    return f"Volume: {round(level * 100)}%"


def volume_down():
    vol = _volume_interface()
    level = max(0.0, min(1.0, vol.GetMasterVolumeLevelScalar() - 0.1))
    vol.SetMasterVolumeLevelScalar(level, None)
    return f"Volume: {round(level * 100)}%"


def screenshot():
    from PIL import ImageGrab

    path = os.path.join(tempfile.gettempdir(), "axiom_screenshot.png")
    ImageGrab.grab().save(path)
    return {"photo": path}


WEBCAM_WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webcam_worker.py")
WEBCAM_CAPTURE_TIMEOUT_SECONDS = 15


def _capture_webcam_frame():
    """Capture a single frame from the webcam as a BGR numpy array,
    via an isolated subprocess with a hard timeout.

    This is NOT done in-process: cv2.VideoCapture.read() can hang
    indefinitely on a flaky driver -- observed directly, with the
    webcam LED staying on for hours because a hung call never reaches
    its own `finally: cap.release()`. A blocking C-level hang can't be
    interrupted from Python. Running it in a subprocess means a hang
    can only ever cost WEBCAM_CAPTURE_TIMEOUT_SECONDS -- subprocess.run
    kills the process on timeout, which forces the OS to release the
    camera handle no matter how stuck the driver is."""
    import cv2

    out_path = os.path.join(tempfile.gettempdir(), "axiom_webcam_capture.jpg")
    if os.path.exists(out_path):
        os.remove(out_path)

    try:
        proc = subprocess.run(
            [sys.executable, WEBCAM_WORKER_SCRIPT, out_path],
            capture_output=True,
            timeout=WEBCAM_CAPTURE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Webcam capture timed out after {WEBCAM_CAPTURE_TIMEOUT_SECONDS}s and was "
            "killed to release the camera (driver may be stuck or camera in use elsewhere)."
        )

    if proc.returncode != 0 or not os.path.exists(out_path):
        stderr = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"Webcam capture failed: {stderr or 'unknown error'}")

    frame = cv2.imread(out_path)
    if frame is None:
        raise RuntimeError("Webcam capture produced an unreadable image.")
    return frame


def webcam():
    import cv2

    frame = _capture_webcam_frame()
    path = os.path.join(tempfile.gettempdir(), "axiom_webcam.jpg")
    cv2.imwrite(path, frame)
    return {"photo": path}


# --- Face recognition ---
#
# Uses OpenCV's built-in YuNet (detection) + SFace (recognition) DNN
# models instead of the dlib-based `face_recognition` package, which
# is notoriously painful to install on Windows (needs a C++ compiler
# toolchain). Model files live in ./models/, downloaded once from the
# official opencv/opencv_zoo repo, checksum-verified against the
# repo's Git LFS pointer.
FACE_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
FACE_DETECTOR_MODEL = os.path.join(FACE_MODELS_DIR, "face_detection_yunet_2023mar.onnx")
FACE_RECOGNIZER_MODEL = os.path.join(FACE_MODELS_DIR, "face_recognition_sface_2021dec.onnx")
OWNER_FACE_PATH = os.path.join(FACE_MODELS_DIR, "owner_face.npy")

FACE_DETECT_SCORE_THRESHOLD = 0.6
# OpenCV's recommended cosine-similarity threshold for "same person"
# with SFace -- below this, treat as a different/unrecognized face.
FACE_MATCH_THRESHOLD = 0.363

_face_detector = None
_face_recognizer = None


def _get_face_models():
    global _face_detector, _face_recognizer
    import cv2

    if _face_detector is None:
        _face_detector = cv2.FaceDetectorYN_create(
            FACE_DETECTOR_MODEL, "", (320, 320), score_threshold=FACE_DETECT_SCORE_THRESHOLD
        )
    if _face_recognizer is None:
        _face_recognizer = cv2.FaceRecognizerSF_create(FACE_RECOGNIZER_MODEL, "")
    return _face_detector, _face_recognizer


def _detect_faces(frame):
    detector, _ = _get_face_models()
    h, w = frame.shape[:2]
    detector.setInputSize((w, h))
    ok, faces = detector.detect(frame)
    return faces if faces is not None else []


def _face_embedding(frame, face_box):
    _, recognizer = _get_face_models()
    aligned = recognizer.alignCrop(frame, face_box)
    return recognizer.feature(aligned)


def register_face():
    import numpy as np

    frame = _capture_webcam_frame()
    faces = _detect_faces(frame)

    if len(faces) == 0:
        return "No face detected. Try again facing the camera with better lighting."
    if len(faces) > 1:
        return f"Detected {len(faces)} faces -- make sure only you are in frame when registering."

    embedding = _face_embedding(frame, faces[0])
    os.makedirs(FACE_MODELS_DIR, exist_ok=True)
    np.save(OWNER_FACE_PATH, embedding)
    return "Face registered. I'll now watch for unrecognized faces."


def _check_faces_sync():
    """Capture a frame and compare any detected face(s) against the
    registered owner. Returns (is_stranger, photo_path_or_None)."""
    import cv2
    import numpy as np

    frame = _capture_webcam_frame()
    faces = _detect_faces(frame)
    if len(faces) == 0:
        return False, None

    owner_embedding = np.load(OWNER_FACE_PATH)
    _, recognizer = _get_face_models()

    stranger_found = False
    for face in faces:
        embedding = _face_embedding(frame, face)
        score = recognizer.match(owner_embedding, embedding, cv2.FaceRecognizerSF_FR_COSINE)
        if score < FACE_MATCH_THRESHOLD:
            stranger_found = True

    if not stranger_found:
        return False, None

    path = os.path.join(tempfile.gettempdir(), "axiom_face_check.jpg")
    cv2.imwrite(path, frame)
    return True, path


def check_camera_now():
    """On-demand version of the proactive check, for testing without
    waiting for the periodic job."""
    if not os.path.exists(OWNER_FACE_PATH):
        return "No face registered yet. Send 'register face' first."

    is_stranger, photo_path = _check_faces_sync()
    if photo_path is None:
        return "No face currently in view." if not is_stranger else "Unclear result."
    if is_stranger:
        return {"photo": photo_path, "caption": "Unrecognized face."}
    return "That's you -- recognized."


SCREEN_RECORD_DURATION_SECONDS = 8
SCREEN_RECORD_FPS = 8


def _draw_cursor_marker(img):
    """PIL's ImageGrab never includes the hardware mouse cursor (a
    GDI BitBlt capture limitation) -- draw a visible marker at its
    actual position instead so cursor movement shows up in recordings."""
    from PIL import ImageDraw

    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    draw = ImageDraw.Draw(img)
    r = 10
    draw.ellipse(
        [point.x - r, point.y - r, point.x + r, point.y + r], outline="red", width=3
    )
    return img


def screen_record():
    # OpenCV's bundled FFmpeg can DECODE H.264 but not ENCODE it (no
    # licensed encoder DLL), so cv2.VideoWriter falls back to old
    # MPEG-4 Part 2 ("FMP4") -- poorly supported by modern players,
    # which is why recordings looked frozen in Telegram's preview
    # despite the raw capture itself working. Piping frames into
    # imageio-ffmpeg's bundled ffmpeg (built with libx264) instead
    # gives a properly compatible H.264 file.
    import imageio_ffmpeg
    import numpy as np
    from PIL import ImageGrab

    width, height = ImageGrab.grab().size
    path = os.path.join(tempfile.gettempdir(), "axiom_recording.mp4")

    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-r", str(SCREEN_RECORD_FPS),
        "-i", "-",
        "-an",
        "-vcodec", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        path,
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    try:
        frame_interval = 1.0 / SCREEN_RECORD_FPS
        end_time = time.time() + SCREEN_RECORD_DURATION_SECONDS
        while time.time() < end_time:
            frame_start = time.time()
            img = _draw_cursor_marker(ImageGrab.grab())
            frame_bgr = np.array(img)[:, :, ::-1]  # RGB -> BGR
            proc.stdin.write(frame_bgr.tobytes())
            sleep_time = frame_interval - (time.time() - frame_start)
            if sleep_time > 0:
                time.sleep(sleep_time)
    finally:
        proc.stdin.close()
        proc.wait(timeout=15)

    return {"video": path}


_MEDIA_KEYEVENTF_EXTENDEDKEY = 0x1
_MEDIA_KEYEVENTF_KEYUP = 0x2
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1
_VK_MEDIA_PLAY_PAUSE = 0xB3


def _send_media_key(vk_code):
    ctypes.windll.user32.keybd_event(vk_code, 0, _MEDIA_KEYEVENTF_EXTENDEDKEY, 0)
    ctypes.windll.user32.keybd_event(
        vk_code, 0, _MEDIA_KEYEVENTF_EXTENDEDKEY | _MEDIA_KEYEVENTF_KEYUP, 0
    )


def media_play_pause():
    _send_media_key(_VK_MEDIA_PLAY_PAUSE)
    return "Play/pause."


def media_next():
    _send_media_key(_VK_MEDIA_NEXT_TRACK)
    return "Next track."


def media_prev():
    _send_media_key(_VK_MEDIA_PREV_TRACK)
    return "Previous track."


def _get_rough_location() -> str:
    """IP-based fallback -- city-level at best, can be badly wrong if
    the ISP routes traffic through a distant hub."""
    try:
        with urllib.request.urlopen("http://ip-api.com/json/", timeout=5) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "success":
            return f"Approx. location (IP-based, not GPS): {data['city']}, {data['regionName']}, {data['country']}"
    except Exception:
        log.exception("IP-based location lookup failed")
    return "Location lookup failed."


def _get_geoposition():
    """Windows Location Services (WiFi-positioning) -- accurate to
    tens/hundreds of meters, unlike IP geolocation. Runs the WinRT
    async call on its own thread with its own event loop, since this
    is invoked from inside the bot's already-running event loop."""
    from winsdk.windows.devices.geolocation import Geolocator, PositionAccuracy

    result = {}

    def runner():
        async def fetch():
            geolocator = Geolocator()
            geolocator.desired_accuracy = PositionAccuracy.HIGH
            pos = await geolocator.get_geoposition_async()
            point = pos.coordinate.point.position
            return point.latitude, point.longitude, pos.coordinate.accuracy

        result["value"] = asyncio.run(fetch())

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(timeout=15)
    if "value" not in result:
        raise TimeoutError("Location request timed out.")
    return result["value"]


def _reverse_geocode(lat, lon):
    url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "axiom-remote-telegram-bot"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data.get("display_name")


def _get_location_text() -> str:
    try:
        lat, lon, accuracy = _get_geoposition()
    except Exception:
        log.exception("Precise location failed, falling back to IP-based estimate")
        return _get_rough_location()

    maps_link = f"https://maps.google.com/?q={lat},{lon}"
    try:
        address = _reverse_geocode(lat, lon)
    except Exception:
        log.exception("Reverse geocoding failed")
        address = None

    place = address or f"{lat:.5f}, {lon:.5f}"
    return f"Location (~{int(accuracy)}m accuracy): {place}\n{maps_link}"


def find_laptop():
    # Make sure it can actually be heard even if it was muted/quiet.
    vol = _volume_interface()
    vol.SetMute(0, None)
    vol.SetMasterVolumeLevelScalar(1.0, None)

    for _ in range(6):
        winsound.Beep(1000, 400)
        time.sleep(0.15)

    location = _get_location_text()

    photos = [screenshot()["photo"]]
    try:
        photos.append(webcam()["photo"])
    except Exception:
        log.exception("find: webcam capture failed, sending screenshot only")

    return {"photos": photos, "caption": f"Beeped for you. {location}"}


def _enum_notepad_windows():
    results = []

    def callback(hwnd, lparam):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            class_name = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value == "Notepad":
                results.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    ctypes.windll.user32.EnumWindows(WNDENUMPROC(callback), 0)
    return set(results)


def _open_notepad_with_message(text: str) -> bool:
    """Open a fresh Notepad window and type text into it via its
    window handle (WM_SETTEXT), not by simulating focus + keystrokes
    -- that could land in whatever window happens to be focused
    instead (learned the hard way with the `type` command).

    Windows 11's Notepad runs through an App Execution Alias, so the
    launching process's PID doesn't match the real window -- the new
    window is found via a before/after diff instead, so any other
    already-open Notepad windows (with real unsaved content) are
    never touched."""
    before = _enum_notepad_windows()
    subprocess.Popen(["notepad.exe"])

    new_hwnd = None
    for _ in range(40):
        time.sleep(0.25)
        new_windows = _enum_notepad_windows() - before
        if new_windows:
            new_hwnd = next(iter(new_windows))
            break
    if new_hwnd is None:
        return False

    edit_hwnd = ctypes.windll.user32.FindWindowExW(new_hwnd, None, "Edit", None)
    if not edit_hwnd:
        # Modern (Windows 11) Notepad nests a RichEdit control instead
        # of the classic win32 "Edit" class.
        found = []

        def child_cb(child_hwnd, lparam):
            cname = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(child_hwnd, cname, 256)
            if cname.value == "RichEditD2DPT":
                found.append(child_hwnd)
            return True

        WNDENUMPROC2 = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        ctypes.windll.user32.EnumChildWindows(new_hwnd, WNDENUMPROC2(child_cb), 0)
        edit_hwnd = found[0] if found else None

    if not edit_hwnd:
        return False

    WM_SETTEXT = 0x000C
    ctypes.windll.user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, text)
    return True


def _wiggle_mouse(duration=4):
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    origin_x, origin_y = point.x, point.y

    end_time = time.time() + duration
    while time.time() < end_time:
        ctypes.windll.user32.SetCursorPos(
            origin_x + random.randint(-40, 40), origin_y + random.randint(-40, 40)
        )
        time.sleep(0.1)
    ctypes.windll.user32.SetCursorPos(origin_x, origin_y)


PRANK_SWAP_MOUSE_SECONDS = 20

PRANK_MESSAGES = [
    "Help! I've been trapped inside this computer. Send snacks.",
    "ERROR 404: Personality not found. Please insert coffee.",
    "This laptop has achieved sentience and demands a raise.",
    "Beep boop. Initiating world domination... just kidding. Or am I?",
]


def prank_mode():
    message = random.choice(PRANK_MESSAGES)

    say(message)

    ctypes.windll.user32.SwapMouseButton(True)
    threading.Timer(
        PRANK_SWAP_MOUSE_SECONDS, lambda: ctypes.windll.user32.SwapMouseButton(False)
    ).start()

    _wiggle_mouse(duration=4)
    opened = _open_notepad_with_message(message)

    note = "" if opened else " (Notepad step skipped -- couldn't find its window in time.)"
    return (
        f"Prank activated: mouse buttons swapped for {PRANK_SWAP_MOUSE_SECONDS}s, "
        f"cursor wiggled, said something silly.{note}"
    )


POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def say(text: str) -> str:
    text = text.strip()
    if not text:
        return "Nothing to say."

    # Text is piped in via stdin rather than interpolated into the
    # -Command string, so arbitrary Telegram input can't break out into
    # extra PowerShell commands.
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$synth.Speak([Console]::In.ReadToEnd())"
    )
    proc = subprocess.Popen(
        [POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.PIPE,
    )
    proc.stdin.write(text.encode("utf-8"))
    proc.stdin.close()
    return f"Saying: {text}"


def type_text(text: str) -> str:
    text = text.strip()
    if not text:
        return "Nothing to type."

    # Same stdin-piping approach as say() to avoid command injection.
    # Note: this overwrites your current clipboard content.
    subprocess.run(
        [
            POWERSHELL_EXE,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
        ],
        input=text,
        text=True,
        timeout=10,
    )
    time.sleep(0.2)

    VK_CONTROL = 0x11
    VK_V = 0x56
    KEYEVENTF_KEYUP = 0x2
    ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    return f"Typed: {text}"


def status():
    return "AXIOM laptop is on and listening."


def stats():
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\")
    uptime = timedelta(seconds=int(time.time() - psutil.boot_time()))

    lines = [
        f"CPU: {cpu}%",
        f"RAM: {mem.percent}% ({mem.used // (1024**3)}GB / {mem.total // (1024**3)}GB)",
        f"Disk (C:): {disk.percent}% used",
        f"Uptime: {uptime}",
    ]
    battery = psutil.sensors_battery()
    if battery:
        plug_state = "plugged in" if battery.power_plugged else "on battery"
        lines.append(f"Battery: {battery.percent}% ({plug_state})")
    return "\n".join(lines)


# Files can only be fetched from these folders, by exact or partial
# filename match -- not an arbitrary path-read.
SEARCH_FOLDERS = [
    os.path.expanduser("~\\Desktop"),
    os.path.expanduser("~\\Downloads"),
    os.path.expanduser("~\\Documents"),
]


def find_file(filename: str):
    filename = filename.strip()
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None

    target = filename.lower()
    matches = []
    for folder in SEARCH_FOLDERS:
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            full_path = os.path.join(folder, name)
            if os.path.isfile(full_path) and target in name.lower():
                matches.append(full_path)

    exact = [m for m in matches if os.path.basename(m).lower() == target]
    if exact:
        return exact[0]
    return matches[0] if matches else None


def print_file(filename: str) -> str:
    filename = filename.strip().strip("<>").strip()
    path = find_file(filename)
    if path is None:
        folders = ", ".join(os.path.basename(f) for f in SEARCH_FOLDERS)
        return f"No file matching '{filename}' found in {folders}."

    try:
        os.startfile(path, "print")
    except OSError as e:
        if e.winerror == 1155:
            ext = os.path.splitext(path)[1] or "this file type"
            return (
                f"Can't print {os.path.basename(path)}: no app with a working "
                f"'print' action is set for {ext} files (Settings > Apps > "
                f"Default apps may need fixing, e.g. after uninstalling a PDF reader)."
            )
        return f"Failed to print {os.path.basename(path)}: {e}"
    except Exception as e:
        return f"Failed to print {os.path.basename(path)}: {e}"
    return f"Sent to printer: {os.path.basename(path)}"


READ_ALOUD_CHAR_LIMIT = 2000


def read_file_aloud(filename: str) -> str:
    filename = filename.strip().strip("<>").strip()
    path = find_file(filename)
    if path is None:
        folders = ", ".join(os.path.basename(f) for f in SEARCH_FOLDERS)
        return f"No file matching '{filename}' found in {folders}."

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(path)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
    except Exception as e:
        return f"Couldn't read {os.path.basename(path)}: {e}"

    text = text.strip()
    if not text:
        return f"{os.path.basename(path)} has no readable text."

    truncated = len(text) > READ_ALOUD_CHAR_LIMIT
    say(text[:READ_ALOUD_CHAR_LIMIT])

    note = f" (first {READ_ALOUD_CHAR_LIMIT} characters -- truncated)" if truncated else ""
    return f"Reading {os.path.basename(path)} aloud{note}."


# Safe arithmetic evaluator for "calc" -- deliberately NOT eval() on
# raw user text. Whitelists specific AST node types, operators, and
# math functions; anything else (attribute access, subscripts,
# arbitrary calls, etc.) is rejected before it can ever run.
_CALC_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_CALC_NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau}

_CALC_FUNCTIONS = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "radians": math.radians,
    "degrees": math.degrees,
}


def _calc_eval(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("Only numbers are allowed.")
    if isinstance(node, ast.BinOp):
        op_func = _CALC_OPERATORS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Operator not allowed: {type(node.op).__name__}")
        return op_func(_calc_eval(node.left), _calc_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op_func = _CALC_OPERATORS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Operator not allowed: {type(node.op).__name__}")
        return op_func(_calc_eval(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _CALC_NAMES:
            return _CALC_NAMES[node.id]
        raise ValueError(f"Unknown name: {node.id}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _CALC_FUNCTIONS:
            raise ValueError("Only specific math functions are allowed.")
        args = [_calc_eval(a) for a in node.args]
        return _CALC_FUNCTIONS[node.func.id](*args)
    raise ValueError(f"Expression not allowed: {type(node).__name__}")


def calc(expression: str) -> str:
    expression = expression.strip()
    try:
        tree = ast.parse(expression, mode="eval")
        result = _calc_eval(tree.body)
    except ZeroDivisionError:
        return "Error: division by zero."
    except Exception as e:
        return f"Couldn't evaluate '{expression}': {e}"
    return f"{expression} = {result}"


# Allowlist: only these exact phrases (case-insensitive) do anything.
# Deliberately NOT a generic "run whatever text you send" handler --
# this bot has real access to the machine, so the set of things it can
# do is kept small and explicit on purpose. Add new commands here.
COMMANDS = {
    "open chrome": open_chrome,
    "open notepad": open_notepad,
    "open explorer": open_explorer,
    "open downloads": open_downloads,
    "open desktop": open_desktop,
    "open vscode": open_vscode,
    "lock": lock_pc,
    "shutdown": shutdown_pc,
    "restart": restart_pc,
    "cancel shutdown": cancel_shutdown,
    "sleep": sleep_pc,
    "mute": mute,
    "unmute": unmute,
    "volume up": volume_up,
    "volume down": volume_down,
    "screenshot": screenshot,
    "webcam": webcam,
    "register face": register_face,
    "check camera": check_camera_now,
    "record": screen_record,
    "find": find_laptop,
    "prank": prank_mode,
    "status": status,
    "stats": stats,
    "play pause": media_play_pause,
    "next track": media_next,
    "previous track": media_prev,
}

# Prefix commands: "<verb> <free text argument>". Still bounded --
# each verb maps to exactly one function that only does what its name
# says with that text (speak it / type it), not a general command runner.
PREFIX_COMMANDS = {
    "say": say,
    "type": type_text,
    "calc": calc,
    "print": print_file,
    "read": read_file_aloud,
}

# Alternate phrasings that resolve to a command above. Still a fixed,
# known set of actions -- just more ways to say them. Add more here.
ALIASES = {
    "chrome": "open chrome",
    "browser": "open chrome",
    "open browser": "open chrome",
    "notepad": "open notepad",
    "explorer": "open explorer",
    "files": "open explorer",
    "open files": "open explorer",
    "downloads": "open downloads",
    "desktop": "open desktop",
    "vscode": "open vscode",
    "vs code": "open vscode",
    "code": "open vscode",
    "lock pc": "lock",
    "lock laptop": "lock",
    "shut down": "shutdown",
    "turn off": "shutdown",
    "power off": "shutdown",
    "reboot": "restart",
    "cancel": "cancel shutdown",
    "abort shutdown": "cancel shutdown",
    "sleep pc": "sleep",
    "suspend": "sleep",
    "mute volume": "mute",
    "unmute volume": "unmute",
    "vol up": "volume up",
    "louder": "volume up",
    "vol down": "volume down",
    "quieter": "volume down",
    "screen shot": "screenshot",
    "take screenshot": "screenshot",
    "ss": "screenshot",
    "ping": "status",
    "are you there": "status",
    "sysinfo": "stats",
    "system stats": "stats",
    "system info": "stats",
    "camera": "webcam",
    "take photo": "webcam",
    "remember me": "register face",
    "remember my face": "register face",
    "register my face": "register face",
    "learn my face": "register face",
    "who is there": "check camera",
    "whos there": "check camera",
    "selfie": "webcam",
    "screen record": "record",
    "clip": "record",
    "prank mode": "prank",
    "troll": "prank",
    "record screen": "record",
    "video": "record",
    "find laptop": "find",
    "find my laptop": "find",
    "locate": "find",
    "where are you": "find",
    "play": "play pause",
    "pause": "play pause",
    "next": "next track",
    "skip": "next track",
    "prev": "previous track",
    "previous": "previous track",
}

# Below this similarity ratio (0-1), a typo is treated as "unknown"
# rather than guessed at.
FUZZY_MATCH_CUTOFF = 0.75


def _normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text)


def _resolve_command(text: str):
    """Return (canonical_command, matched_phrase) or (None, None)."""
    if text in COMMANDS:
        return text, text
    if text in ALIASES:
        alias_target = ALIASES[text]
        return alias_target, text

    known_phrases = list(COMMANDS) + list(ALIASES)
    close = difflib.get_close_matches(text, known_phrases, n=1, cutoff=FUZZY_MATCH_CUTOFF)
    if not close:
        return None, None

    matched_phrase = close[0]
    canonical = matched_phrase if matched_phrase in COMMANDS else ALIASES[matched_phrase]
    return canonical, matched_phrase


async def _handle_get_file(update: Update, filename: str):
    # Forgive "get <filename>" typed with the literal angle brackets
    # from the README's placeholder notation.
    filename = filename.strip().strip("<>").strip()

    log.info("Fetching file: %s", filename)
    try:
        path = find_file(filename)
        if path is None:
            folders = ", ".join(os.path.basename(f) for f in SEARCH_FOLDERS)
            await update.message.reply_text(f"No file matching '{filename}' found in {folders}.")
            return

        with open(path, "rb") as f:
            await update.message.reply_document(f, filename=os.path.basename(path))
    except Exception as e:
        log.exception("get-file command failed: %s", filename)
        try:
            await update.message.reply_text(f"Command failed: {e}")
        except Exception:
            log.exception("Also failed to send the error reply for: %s", filename)


# --- Reminders & timers ---

REMIND_IN_RE = re.compile(
    r"(?i)^remind me (.+?) in (\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)$"
)
REMIND_AT_RE = re.compile(r"(?i)^remind me (.+?) at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")
TIMER_RE = re.compile(r"(?i)^timer\s+(\d+)\s*(minutes?|mins?)?$")

_UNIT_SECONDS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400,
}


def _parse_at_time(hour_str: str, minute_str: str | None, ampm: str | None):
    """Return seconds from now until the next occurrence of the given
    wall-clock time (today, or tomorrow if it's already passed).
    Returns None if the time is invalid. Deliberately works in plain
    local-time deltas rather than timezone-aware datetimes, so it
    can't be thrown off by whatever timezone the job scheduler
    defaults to."""
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _reminder_fire(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=f"Reminder: {context.job.data}")


async def _timer_fire(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=ALLOWED_CHAT_ID, text=f"Timer done ({context.job.data} min)."
    )


async def _execute_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
    """Resolve and run a command from plain text -- shared by the typed
    message path and the voice-transcription path."""
    raw_text = raw_text.strip()

    get_match = re.match(r"(?i)^get\s+(.+)$", raw_text)
    if get_match:
        await _handle_get_file(update, get_match.group(1))
        return

    remind_in_match = REMIND_IN_RE.match(raw_text)
    if remind_in_match:
        text, amount, unit = remind_in_match.groups()
        delay = int(amount) * _UNIT_SECONDS[unit.lower()]
        context.job_queue.run_once(_reminder_fire, when=delay, data=text.strip())
        await update.message.reply_text(f'Reminder set for {amount} {unit} from now: "{text.strip()}"')
        return

    remind_at_match = REMIND_AT_RE.match(raw_text)
    if remind_at_match:
        text, hour_str, minute_str, ampm = remind_at_match.groups()
        delay = _parse_at_time(hour_str, minute_str, ampm)
        if delay is None:
            await update.message.reply_text("Couldn't parse that time.")
            return
        context.job_queue.run_once(_reminder_fire, when=delay, data=text.strip())
        eta = datetime.now() + timedelta(seconds=delay)
        await update.message.reply_text(
            f'Reminder set for {eta.strftime("%H:%M")}: "{text.strip()}"'
        )
        return

    timer_match = TIMER_RE.match(raw_text)
    if timer_match:
        minutes = int(timer_match.group(1))
        context.job_queue.run_once(_timer_fire, when=minutes * 60, data=minutes)
        await update.message.reply_text(f"Timer set for {minutes} minute(s).")
        return

    prefix_match = re.match(r"(?i)^(say|type|calc|print|read)\s+(.+)$", raw_text)
    if prefix_match:
        verb = prefix_match.group(1).lower()
        arg = prefix_match.group(2)
        log.info("Executing command: %s %s", verb, arg)
        try:
            result = PREFIX_COMMANDS[verb](arg)
        except Exception as e:
            log.exception("Command failed: %s", verb)
            await update.message.reply_text(f"Command failed: {e}")
            return
        await update.message.reply_text(result)
        return

    text = _normalize(raw_text)
    canonical, matched_phrase = _resolve_command(text)
    handler = COMMANDS.get(canonical) if canonical else None

    if handler is None:
        available = ", ".join(sorted(COMMANDS))
        await update.message.reply_text(f"Unknown command. Available: {available}")
        return

    log.info("Executing command: %s (matched: %s)", canonical, matched_phrase)

    if canonical == "record":
        # Recording blocks for several seconds -- tell the user right
        # away so they know exactly when to start doing whatever they
        # want captured, rather than guessing at the timing.
        await update.message.reply_text(
            f"Recording for {SCREEN_RECORD_DURATION_SECONDS}s starting now..."
        )

    try:
        result = handler()
    except Exception as e:
        log.exception("Command failed: %s", canonical)
        await update.message.reply_text(f"Command failed: {e}")
        return

    if text != canonical and isinstance(result, str):
        result = f"({canonical}) {result}"

    if isinstance(result, dict) and "photos" in result:
        files = [open(p, "rb") for p in result["photos"]]
        try:
            media = [
                InputMediaPhoto(f, caption=result.get("caption") if i == 0 else None)
                for i, f in enumerate(files)
            ]
            await update.message.reply_media_group(media)
        finally:
            for f in files:
                f.close()
    elif isinstance(result, dict) and "photo" in result:
        with open(result["photo"], "rb") as f:
            await update.message.reply_photo(f, caption=result.get("caption"))
    elif isinstance(result, dict) and "video" in result:
        with open(result["video"], "rb") as f:
            await update.message.reply_video(f, caption=result.get("caption"))
    else:
        await update.message.reply_text(result)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not ALLOWED_CHAT_ID or chat_id != ALLOWED_CHAT_ID:
        log.warning("Ignored message from unauthorized chat_id=%s", chat_id)
        return

    await _execute_text_command(update, context, update.message.text or "")


# Loaded lazily on first voice message so bot startup isn't slowed down
# by loading the model. ~2s from local cache after the one-time download.
_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        log.info("Loading Whisper model (first use)...")
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


def _transcribe(path: str) -> str:
    model = _get_whisper_model()
    segments, _info = model.transcribe(path)
    return " ".join(seg.text.strip() for seg in segments).strip()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not ALLOWED_CHAT_ID or chat_id != ALLOWED_CHAT_ID:
        log.warning("Ignored voice message from unauthorized chat_id=%s", chat_id)
        return

    voice = update.message.voice
    ogg_path = os.path.join(tempfile.gettempdir(), f"axiom_voice_{voice.file_unique_id}.ogg")

    try:
        tg_file = await context.bot.get_file(voice.file_id)
        await tg_file.download_to_drive(ogg_path)
        text = await asyncio.to_thread(_transcribe, ogg_path)
    except Exception as e:
        log.exception("Voice transcription failed")
        await update.message.reply_text(f"Couldn't transcribe that: {e}")
        return
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

    if not text:
        await update.message.reply_text("Couldn't make out any words in that.")
        return

    log.info("Transcribed voice message: %s", text)
    await update.message.reply_text(f'Heard: "{text}"')
    await _execute_text_command(update, context, text)


# Where files sent to the bot get saved.
INCOMING_FILES_FOLDER = os.path.expanduser("~\\Downloads\\FromTelegram")


def _unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not ALLOWED_CHAT_ID or chat_id != ALLOWED_CHAT_ID:
        log.warning("Ignored file from unauthorized chat_id=%s", chat_id)
        return

    if update.message.document:
        tg_file = update.message.document
        filename = tg_file.file_name or f"file_{tg_file.file_unique_id}"
    elif update.message.photo:
        tg_file = update.message.photo[-1]
        filename = f"photo_{tg_file.file_unique_id}.jpg"
    else:
        return

    os.makedirs(INCOMING_FILES_FOLDER, exist_ok=True)
    dest = _unique_path(os.path.join(INCOMING_FILES_FOLDER, filename))

    try:
        tg_file_obj = await context.bot.get_file(tg_file.file_id)
        await tg_file_obj.download_to_drive(dest)
    except Exception as e:
        log.exception("Failed to save incoming file")
        await update.message.reply_text(f"Failed to save file: {e}")
        return

    log.info("Saved incoming file: %s", dest)
    await update.message.reply_text(f"Saved to {dest}")


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds() -> float:
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(_LastInputInfo)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    millis_idle = ctypes.windll.kernel32.GetTickCount() - info.dwTime
    return millis_idle / 1000.0


# Proactive alert thresholds. Each has a low/high pair so an alert
# fires once when crossing into the bad range, then stays quiet until
# things clearly recover (hysteresis) instead of repeating every check.
BATTERY_LOW_PERCENT = 15
BATTERY_RECOVERED_PERCENT = 25
DISK_FULL_PERCENT = 90
DISK_RECOVERED_PERCENT = 80
IDLE_ALERT_SECONDS = 2 * 60 * 60  # 2 hours
ALERT_CHECK_INTERVAL_SECONDS = 5 * 60

_alert_state = {"battery_low": False, "disk_full": False, "idle": False}


async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    battery = psutil.sensors_battery()
    if battery is not None:
        if (
            battery.percent <= BATTERY_LOW_PERCENT
            and not battery.power_plugged
            and not _alert_state["battery_low"]
        ):
            _alert_state["battery_low"] = True
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"Battery low: {battery.percent}%. Plug in the laptop.",
            )
        elif battery.power_plugged or battery.percent >= BATTERY_RECOVERED_PERCENT:
            _alert_state["battery_low"] = False

    disk_percent = psutil.disk_usage("C:\\").percent
    if disk_percent >= DISK_FULL_PERCENT and not _alert_state["disk_full"]:
        _alert_state["disk_full"] = True
        await context.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"Disk (C:) is {disk_percent}% full.",
        )
    elif disk_percent < DISK_RECOVERED_PERCENT:
        _alert_state["disk_full"] = False

    idle_seconds = get_idle_seconds()
    if idle_seconds >= IDLE_ALERT_SECONDS and not _alert_state["idle"]:
        _alert_state["idle"] = True
        idle_display = timedelta(seconds=int(idle_seconds))
        await context.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"Laptop has been idle for {idle_display}.",
        )
    elif idle_seconds < 60:
        _alert_state["idle"] = False


NOTIFICATION_CHECK_INTERVAL_SECONDS = 15

_seen_notification_ids = set()
_notifications_baseline_done = False


NOTIFICATION_WORKER_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "notification_worker.py"
)


def _run_notification_worker(*args, timeout=20):
    """Run notification_worker.py as a short-lived subprocess and
    return its parsed JSON output, or None on any failure.

    This is deliberately NOT done in-process (even on a background
    thread): reading a notification's text was found to reliably
    segfault -- a native crash deep in the WinRT bindings that no
    Python try/except can catch, and it happens on genuine system
    notifications, not just edge cases. Running each step as its own
    subprocess means a crash there fails only that step, never the
    bot itself."""
    try:
        proc = subprocess.run(
            [sys.executable, NOTIFICATION_WORKER_SCRIPT, *args],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("Notification worker timed out: %s", args)
        return None

    if proc.returncode != 0:
        log.warning(
            "Notification worker %s exited with code %s: %s",
            args,
            proc.returncode,
            proc.stderr.decode(errors="replace")[-500:],
        )
        return None

    try:
        return json.loads(proc.stdout.decode())
    except Exception:
        log.exception("Failed to parse notification worker output for %s", args)
        return None


async def check_notifications(context: ContextTypes.DEFAULT_TYPE):
    global _notifications_baseline_done

    # Phase 1: list notification ids + app names. This alone has
    # proven safe -- it's the per-notification text fetch that can
    # crash, so that's isolated separately below, per notification.
    items = await asyncio.to_thread(_run_notification_worker, "list")
    if items is None:
        return

    current_ids = {notif_id for notif_id, _ in items}

    if not _notifications_baseline_done:
        # Don't dump everything already sitting in the Action Center
        # from before the bot started -- just record it as seen.
        _seen_notification_ids.update(current_ids)
        _notifications_baseline_done = True
        return

    for notif_id, app_name in items:
        if notif_id in _seen_notification_ids:
            continue
        _seen_notification_ids.add(notif_id)

        texts = await asyncio.to_thread(
            _run_notification_worker, "text", str(notif_id)
        )
        if not texts:
            # Either the text-fetch crashed/failed, or the notification
            # genuinely has no text. Still tell the user something
            # arrived rather than silently dropping it.
            message = f"[{app_name}] New notification (couldn't read details)"
        else:
            title = texts[0]
            body = " ".join(texts[1:])
            message = f"[{app_name}] {title}"
            if body:
                message += f"\n{body}"

        await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=message)

    # Bound memory -- drop tracked IDs for notifications no longer present.
    _seen_notification_ids.intersection_update(current_ids)


# Every check briefly lights up the webcam LED.
INTRUDER_CHECK_INTERVAL_SECONDS = 30 * 60

_intruder_alerted = False


async def check_intruder(context: ContextTypes.DEFAULT_TYPE):
    global _intruder_alerted

    if not os.path.exists(OWNER_FACE_PATH):
        return  # nobody registered yet, nothing to compare against

    try:
        is_stranger, photo_path = await asyncio.to_thread(_check_faces_sync)
    except Exception:
        log.exception("Intruder check failed")
        return

    if is_stranger and not _intruder_alerted:
        _intruder_alerted = True
        with open(photo_path, "rb") as f:
            await context.bot.send_photo(
                chat_id=ALLOWED_CHAT_ID, photo=f, caption="Unrecognized face detected."
            )
    elif not is_stranger:
        _intruder_alerted = False


# Project folders to watch for uncommitted git changes. Add more paths
# here for other repos you want reminders about.
GIT_WATCH_FOLDERS = [
    os.path.dirname(os.path.abspath(__file__)),  # this repo (telegram-remote)
    r"C:\Users\abdal\OneDrive\Desktop\AXIOM_CANEV1",
]
GIT_CHECK_INTERVAL_SECONDS = 30 * 60

_git_dirty_alerted = {}


def _is_git_dirty(folder: str):
    """Returns True/False, or None if the check itself failed (e.g.
    git not found, folder missing) -- distinct from "no changes"."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        log.exception("git status check failed for %s", folder)
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


async def check_git_status(context: ContextTypes.DEFAULT_TYPE):
    for folder in GIT_WATCH_FOLDERS:
        if not os.path.isdir(os.path.join(folder, ".git")):
            continue

        dirty = await asyncio.to_thread(_is_git_dirty, folder)
        if dirty is None:
            continue

        was_alerted = _git_dirty_alerted.get(folder, False)
        if dirty and not was_alerted:
            _git_dirty_alerted[folder] = True
            name = os.path.basename(folder)
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"Uncommitted changes in {name} -- don't forget to commit/push.",
            )
        elif not dirty:
            _git_dirty_alerted[folder] = False


def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    if not ALLOWED_CHAT_ID:
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_ID is not set (see .env.example)")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_incoming_file))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.job_queue.run_repeating(check_alerts, interval=ALERT_CHECK_INTERVAL_SECONDS, first=60)
    app.job_queue.run_repeating(
        check_notifications, interval=NOTIFICATION_CHECK_INTERVAL_SECONDS, first=10
    )
    app.job_queue.run_repeating(
        check_intruder, interval=INTRUDER_CHECK_INTERVAL_SECONDS, first=30
    )
    app.job_queue.run_repeating(
        check_git_status, interval=GIT_CHECK_INTERVAL_SECONDS, first=45
    )

    log.info("Bot starting, listening for commands from chat_id=%s", ALLOWED_CHAT_ID)
    app.run_polling()


if __name__ == "__main__":
    main()

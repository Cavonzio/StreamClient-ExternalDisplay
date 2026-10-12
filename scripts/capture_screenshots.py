#!/usr/bin/env python3
"""
Capture App Store Connect screenshots of both apps in demo mode.

Usage:
    python3 scripts/capture_screenshots.py                      # everything
    python3 scripts/capture_screenshots.py --schemes Dispatcharr --platforms iPhone,iPad
    python3 scripts/capture_screenshots.py --platforms macOS --skip-build
    python3 scripts/capture_screenshots.py --shots guide,channels --appearance dark

    --schemes     comma-separated: NextPVR, Dispatcharr          (default: both)
    --platforms   comma-separated: iPhone, iPhone-6.5, iPhone-6.3, iPad, tvOS,
                  macOS                                          (default: all)
    --shots       comma-separated shot names, see SHOTS          (default: all)
    --appearance  light or dark                                  (default: light)
    --output      output directory                               (default: screenshots/)
    --skip-build  reuse the apps already built in .derivedData
    --settle      seconds to wait for a page before capturing    (default: 7)

Output: <output>/<scheme>/<platform>/<nn>-<shot>.png, at a size App Store
Connect accepts as is:

    iPhone 6.9"   1320 x 2868   (iPhone 17 Pro Max simulator)
    iPhone 6.5"   1284 x 2778   (iPhone 14 Plus simulator)
    iPhone 6.3"   1206 x 2622   (iPhone 17 Pro simulator)
    iPad 13"      2064 x 2752   (iPad Pro 13-inch (M5) simulator)
    Apple TV      3840 x 2160   (Apple TV 4K simulator)
    Mac           2880 x 1800   (the app window on a solid background)

The Mac captures need a Retina display, and the pointer kept away from the
middle of the screen: the window opens there, and hover highlights show.

Each page is opened with the demo launch arguments (`--demo-mode --demo-tab
...`, see AppState.applyDemoPageArguments), so nothing is tapped or scripted.

The simulators are dedicated ones ("Screenshots ..."), created on first use, so
demo mode never replaces the server of a simulator used for development.

Demo mode saves a "demo" server config, which on a Mac would overwrite the
real one and sync it to iCloud. The macOS app is therefore built apart, under
another bundle identifier and without the iCloud entitlement: it gets its own
sandbox container and can't reach the real app's settings.

Requirements: Xcode, Pillow (`pip install Pillow`), and for macOS the Screen
Recording permission for the terminal running this script.
"""

import argparse
import json
import plistlib
import subprocess
import sys
import time
from pathlib import Path

try:
    from PIL import Image, ImageFilter
except ImportError:
    print("Pillow is required. Install with: pip install Pillow")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "NexusPVR.xcodeproj"
DERIVED_DATA = ROOT / ".derivedData"
# The isolated macOS build (see the module docstring).
MAC_DERIVED_DATA = DERIVED_DATA / "Screenshots-macOS"
MAC_BUNDLE_ID_PREFIX = "screenshots"

ALL_SCHEMES = ["NextPVR", "Dispatcharr"]
ALL_PLATFORMS = ["iPhone", "iPhone-6.5", "iPhone-6.3", "iPad", "tvOS", "macOS"]

# (name, demo launch arguments, schemes it exists in)
# tvOS has no Calendar page: see TVOS_SKIPPED_SHOTS.
SHOTS = [
    ("guide", ["--demo-tab", "Guide"], ALL_SCHEMES),
    ("channels", ["--demo-tab", "Channels"], ALL_SCHEMES),
    ("movies", ["--demo-tab", "On Demand", "--demo-vod", "Movies"], ["Dispatcharr"]),
    ("series", ["--demo-tab", "On Demand", "--demo-vod", "The Quantum Detective"], ["Dispatcharr"]),
    ("recordings", ["--demo-tab", "Recordings"], ALL_SCHEMES),
    ("recorded-series", ["--demo-tab", "Recordings", "--demo-recordings", "The Quantum Detective"], ALL_SCHEMES),
    ("topics", ["--demo-tab", "Topics"], ALL_SCHEMES),
    ("calendar", ["--demo-tab", "Calendar"], ALL_SCHEMES),
]

TVOS_SKIPPED_SHOTS = {"calendar"}

# name, device type, runtime platform, sizes App Store Connect accepts
SIMULATORS = {
    "iPhone": ("Screenshots iPhone 6.9", "iPhone-17-Pro-Max", "iOS",
               {(1320, 2868), (1290, 2796), (1260, 2736)}),
    "iPhone-6.5": ("Screenshots iPhone 6.5", "iPhone-14-Plus", "iOS",
                   {(1284, 2778), (1242, 2688)}),
    "iPhone-6.3": ("Screenshots iPhone 6.3", "iPhone-17-Pro", "iOS",
                   {(1206, 2622), (1179, 2556)}),
    "iPad": ("Screenshots iPad 13", "iPad-Pro-13-inch-M5-12GB", "iOS",
             {(2064, 2752), (2048, 2732)}),
    "tvOS": ("Screenshots Apple TV 4K", "Apple-TV-4K-3rd-generation-4K", "tvOS",
             {(3840, 2160), (1920, 1080)}),
}
MAC_SIZE = (2880, 1800)

# Prints "<window id> <width> <height>" for each of a process's windows.
WINDOW_LIST_SOURCE = """
import CoreGraphics
import Foundation
let pid = Int(CommandLine.arguments[1])!
let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
for w in list where (w[kCGWindowOwnerPID as String] as? Int) == pid && (w[kCGWindowLayer as String] as? Int) == 0 {
    let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
    print(w[kCGWindowNumber as String] as? Int ?? 0, b["Width"] as? Int ?? 0, b["Height"] as? Int ?? 0)
}
"""


def run(*args: str, check: bool = True, quiet: bool = False) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        if not quiet:
            print(result.stdout[-3000:], result.stderr[-3000:], sep="\n", file=sys.stderr)
        raise SystemExit(f"ERROR: {' '.join(args[:4])} ... failed ({result.returncode})")
    return result.stdout


def simctl(*args: str, check: bool = True) -> str:
    return run("xcrun", "simctl", *args, check=check)


# --- Build -----------------------------------------------------------------

def mac_entitlements() -> Path:
    """Sandbox and network only: ad-hoc signing can't carry the iCloud ones."""
    path = MAC_DERIVED_DATA / "Screenshots.entitlements"
    MAC_DERIVED_DATA.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as file:
        plistlib.dump({"com.apple.security.app-sandbox": True,
                       "com.apple.security.network.client": True}, file)
    return path


def xcodebuild_args(scheme: str, platform: str, simulator_id: str | None) -> list[str]:
    args = ["xcodebuild", "-project", str(PROJECT), "-scheme", scheme, "-configuration", "Debug"]
    if platform == "macOS":
        args += ["-destination", "platform=macOS,arch=arm64",
                 "-derivedDataPath", str(MAC_DERIVED_DATA),
                 f"BUNDLE_ID_PREFIX={MAC_BUNDLE_ID_PREFIX}",
                 "CODE_SIGN_IDENTITY=-",
                 f"CODE_SIGN_ENTITLEMENTS={mac_entitlements()}"]
    else:
        sim_platform = "tvOS Simulator" if platform == "tvOS" else "iOS Simulator"
        args += ["-destination", f"platform={sim_platform},id={simulator_id}",
                 "-derivedDataPath", str(DERIVED_DATA)]
    return args


def build(scheme: str, platform: str, simulator_id: str | None, skip_build: bool) -> tuple[Path, str]:
    """Builds the app and returns its path and bundle identifier."""
    args = xcodebuild_args(scheme, platform, simulator_id)
    if not skip_build:
        print(f"  Building {scheme} for {platform}...")
        run(*args, "build")
    settings = json.loads(run(*args, "-showBuildSettings", "-json"))
    for entry in settings:
        s = entry["buildSettings"]
        if s.get("WRAPPER_EXTENSION") == "app" and s.get("PRODUCT_TYPE") == "com.apple.product-type.application":
            app = Path(s["TARGET_BUILD_DIR"]) / s["WRAPPER_NAME"]
            if not app.exists():
                raise SystemExit(f"ERROR: {app} isn't built; run without --skip-build.")
            return app, s["PRODUCT_BUNDLE_IDENTIFIER"]
    raise SystemExit(f"ERROR: no app target in scheme {scheme}.")


# --- Simulators ------------------------------------------------------------

def create_simulator(name: str, device_type: str, runtime_platform: str) -> str:
    """Creates the simulator on the newest installed runtime that has the device."""
    runtimes = json.loads(simctl("list", "runtimes", "-j"))["runtimes"]
    available = [r for r in runtimes if r["isAvailable"] and r["platform"] == runtime_platform]
    available.sort(key=lambda r: [int(n) for n in r["version"].split(".")], reverse=True)
    for runtime in available:
        result = subprocess.run(["xcrun", "simctl", "create", name,
                                 f"com.apple.CoreSimulator.SimDeviceType.{device_type}", runtime["identifier"]],
                                capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  Created simulator {name} ({runtime['name']})")
            return result.stdout.strip()
    raise SystemExit(f"ERROR: no installed {runtime_platform} simulator runtime supports {device_type}.")


def prepare_simulator(platform: str, appearance: str) -> str:
    """Finds or creates the screenshot simulator, boots it and returns its id."""
    name, device_type, runtime_platform, _ = SIMULATORS[platform]
    devices = json.loads(simctl("list", "devices", "-j"))["devices"]
    udid = next((d["udid"] for runtime in devices.values() for d in runtime
                 if d["name"] == name and d["isAvailable"]), None)
    if udid is None:
        udid = create_simulator(name, device_type, runtime_platform)
    simctl("bootstatus", udid, "-b")

    # English system UI (the iPad status bar shows the date), whatever the
    # Mac's own language is. Takes a reboot to apply.
    locale = simctl("spawn", udid, "defaults", "read", "-g", "AppleLocale", check=False).strip()
    if locale != "en_US":
        simctl("spawn", udid, "defaults", "write", "-g", "AppleLanguages", "-array", "en")
        simctl("spawn", udid, "defaults", "write", "-g", "AppleLocale", "en_US")
        simctl("shutdown", udid)
        simctl("bootstatus", udid, "-b")

    # tvOS simulators have neither an appearance switch nor a status bar.
    if platform != "tvOS":
        simctl("ui", udid, "appearance", appearance)
        simctl("status_bar", udid, "override", "--time", "9:41",
               "--batteryState", "charged", "--batteryLevel", "100",
               "--wifiBars", "3", "--cellularBars", "4",
               "--dataNetwork", "wifi", "--cellularMode", "active")
    return udid


def capture_simulator(udid: str, bundle_id: str, arguments: list[str], settle: float, destination: Path):
    simctl("terminate", udid, bundle_id, check=False)
    simctl("launch", udid, bundle_id, "--demo-mode", *arguments)
    time.sleep(settle)
    simctl("io", udid, "screenshot", str(destination))
    simctl("terminate", udid, bundle_id, check=False)
    # App Store Connect refuses an alpha channel.
    Image.open(destination).convert("RGB").save(destination, "PNG")


# --- macOS -----------------------------------------------------------------

def window_list_tool() -> Path:
    tool = MAC_DERIVED_DATA / "window-list"
    if not tool.exists():
        source = MAC_DERIVED_DATA / "window-list.swift"
        source.write_text(WINDOW_LIST_SOURCE)
        run("swiftc", "-O", str(source), "-o", str(tool))
    return tool


def mac_pids(app: Path) -> list[str]:
    return run("pgrep", "-f", f"{app}/Contents/MacOS/", check=False).split()


def quit_mac_app(app: Path):
    for pid in mac_pids(app):
        run("kill", pid, check=False)
    deadline = time.time() + 5
    while mac_pids(app) and time.time() < deadline:
        time.sleep(0.2)


def open_mac_window(app: Path, arguments: list[str], appearance: str) -> str:
    """Launches the app and returns the id of its main window."""
    # Argument-domain defaults: the appearance, without touching the system's.
    style = ["-AppleInterfaceStyle", "Dark"] if appearance == "dark" else ["-NSRequiresAquaSystemAppearance", "YES"]
    tool = window_list_tool()
    for _ in range(2):
        quit_mac_app(app)
        # `--demo-mode` goes last: AppKit reads the arguments as `-key value`
        # pairs, and an unpaired page name counts as a file to open, which
        # keeps the main window from opening.
        run("open", "-n", str(app), "--args", *arguments, *style, "--demo-mode")
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(0.5)
            pids = mac_pids(app)
            if not pids:
                continue
            for line in run(str(tool), pids[0], check=False).splitlines():
                window_id, width, height = line.split()
                if int(width) > 400 and int(height) > 300:
                    return window_id
    quit_mac_app(app)
    raise SystemExit(f"ERROR: {app.name} opened no window.")


def place_mac_window(window: Image.Image, background: tuple) -> Image.Image:
    """Centers the window capture, with a soft shadow, on a solid background."""
    window = window.convert("RGBA")
    limit = (round(MAC_SIZE[0] * 0.9), round(MAC_SIZE[1] * 0.9))
    if window.width > limit[0] or window.height > limit[1]:
        window.thumbnail(limit, Image.LANCZOS)
    x = (MAC_SIZE[0] - window.width) // 2
    y = (MAC_SIZE[1] - window.height) // 2
    canvas = Image.new("RGBA", MAC_SIZE, background + (255,))
    shadow = Image.new("RGBA", MAC_SIZE, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 110), (x, y + 30), window.getchannel("A"))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(45)))
    canvas.alpha_composite(window, (x, y))
    return canvas.convert("RGB")


def capture_mac(app: Path, bundle_id: str, arguments: list[str], appearance: str, settle: float, destination: Path):
    # A fresh defaults domain: the window opens at its default size.
    run("defaults", "delete", bundle_id, check=False)
    window_id = open_mac_window(app, arguments, appearance)
    time.sleep(settle)
    raw = destination.with_suffix(".window.png")
    # `-o`: without the system shadow, whose size depends on the window being
    # active; place_mac_window draws its own.
    result = subprocess.run(["screencapture", "-x", "-o", "-l", window_id, str(raw)], capture_output=True, text=True)
    quit_mac_app(app)
    if result.returncode != 0 or not raw.exists():
        raise SystemExit("ERROR: screencapture failed. Grant Screen Recording to this terminal in "
                         "System Settings > Privacy & Security, then run again.\n" + result.stderr)
    background = (18, 18, 28) if appearance == "dark" else (222, 228, 240)
    place_mac_window(Image.open(raw), background).save(destination, "PNG")
    raw.unlink()


# --- Main ------------------------------------------------------------------

def parse_list(label: str, value: str, allowed: list[str]) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in items if item not in allowed]
    if unknown or not items:
        raise SystemExit(f"ERROR: unknown {label} {', '.join(unknown)} (expected: {', '.join(allowed)}).")
    return items


def main():
    shot_names = [name for name, _, _ in SHOTS]
    parser = argparse.ArgumentParser(description="Capture App Store Connect screenshots in demo mode")
    parser.add_argument("--schemes", default=",".join(ALL_SCHEMES))
    parser.add_argument("--platforms", default=",".join(ALL_PLATFORMS))
    parser.add_argument("--shots", default=",".join(shot_names))
    parser.add_argument("--appearance", choices=["light", "dark"], default="light")
    parser.add_argument("--output", "-o", default=str(ROOT / "screenshots"))
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--settle", type=float, default=7)
    args = parser.parse_args()

    schemes = parse_list("scheme", args.schemes, ALL_SCHEMES)
    platforms = parse_list("platform", args.platforms, ALL_PLATFORMS)
    wanted = parse_list("shot", args.shots, shot_names)
    output = Path(args.output)
    problems: list[str] = []
    total = 0

    simulators = {p: prepare_simulator(p, args.appearance) for p in platforms if p != "macOS"}

    for scheme in schemes:
        for platform in platforms:
            print(f"{scheme} / {platform}")
            udid = simulators.get(platform)
            app, bundle_id = build(scheme, platform, udid, args.skip_build)
            if udid:
                simctl("install", udid, str(app))
            folder = output / scheme / platform
            folder.mkdir(parents=True, exist_ok=True)

            shots = [shot for shot in SHOTS if scheme in shot[2]]
            for index, (name, arguments, _) in enumerate(shots, start=1):
                if name not in wanted or (platform == "tvOS" and name in TVOS_SKIPPED_SHOTS):
                    continue
                destination = folder / f"{index:02d}-{name}.png"
                if udid:
                    capture_simulator(udid, bundle_id, arguments, args.settle, destination)
                    accepted = SIMULATORS[platform][3]
                else:
                    capture_mac(app, bundle_id, arguments, args.appearance, args.settle, destination)
                    accepted = {MAC_SIZE}
                size = Image.open(destination).size
                print(f"    -> {destination.relative_to(output)} ({size[0]}x{size[1]})")
                total += 1
                if size not in accepted:
                    problems.append(f"{destination}: {size[0]}x{size[1]} isn't an accepted {platform} size")

    print(f"\nDone! {total} screenshots saved to {output}/")
    if problems:
        print("\n".join(["", "Size problems:"] + problems))
        sys.exit(1)


if __name__ == "__main__":
    main()

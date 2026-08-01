import os
import struct
from pathlib import Path


ICONSET = Path("/private/tmp/FamilyFinances.iconset")
APP_BUNDLE = Path(
    os.environ.get(
        "FAMILY_FINANCES_APP_BUNDLE",
        Path.home() / "Desktop/Family Finances.app",
    )
)
OUTPUT = APP_BUNDLE / "Contents/Resources/applet.icns"
FILES = (
    (b"icp4", "icon_16x16.png"),
    (b"ic11", "icon_16x16@2x.png"),
    (b"icp5", "icon_32x32.png"),
    (b"ic12", "icon_32x32@2x.png"),
    (b"ic07", "icon_128x128.png"),
    (b"ic13", "icon_128x128@2x.png"),
    (b"ic08", "icon_256x256.png"),
    (b"ic14", "icon_256x256@2x.png"),
    (b"ic09", "icon_512x512.png"),
    (b"ic10", "icon_512x512@2x.png"),
)


chunks = []
for icon_type, filename in FILES:
    data = (ICONSET / filename).read_bytes()
    chunks.append(icon_type + struct.pack(">I", len(data) + 8) + data)

body = b"".join(chunks)
OUTPUT.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)

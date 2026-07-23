"""Generate a multi-size Windows .ico from a PNG. Usage: _make_icon.py in.png out.ico"""
import sys
from PIL import Image

Image.open(sys.argv[1]).save(
    sys.argv[2],
    sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
)
print("icon written:", sys.argv[2])

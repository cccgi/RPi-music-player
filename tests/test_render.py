"""Renderer smoke test: writes a contact sheet of every keycap type."""
import _bootstrap  # noqa: F401
import sys, os
_bootstrap.require("PIL")
from PIL import Image
from player.config import load_config, setup_logging
from player.streamdeck.render import KeyRenderer, Theme
from player.streamdeck import layout
setup_logging('ERROR')

cfg=load_config()
t=cfg.streamdeck.theme
r=KeyRenderer(Theme(bg=t.bg,fg=t.fg,muted=t.muted,accent=t.accent,active=t.active,
                    warn=t.warn,font_regular=t.font_regular,font_bold=t.font_bold))

# Simulate a realistic full panel
tiles=[]
def add(img,name): tiles.append((img,name))

# Row 0
add(r.now_playing("Shine On You Crazy Diamond (Parts I-V)","Pink Floyd"),"np-long")
add(r.now_playing("Teardrop","Massive Attack"),"np-short")
add(r.now_playing("","") ,"np-empty")
add(r.label("Wish You Were Here", sub="album"),"album")
add(r.label("7", sub="of 142"),"queue")
add(r.progress(197.0, 811.0, "play"),"progress")
add(r.volume(62),"vol")
add(r.volume(0, muted=True),"vol-muted")
# Row 1
add(r.volume(None),"vol-nomixer")
add(r.status("Shuffle", True),"shuffle-on")
add(r.status("Repeat", False),"repeat-off")
add(r.status("Consume", False),"consume")
add(r.glyph("play"),"play")
add(r.glyph("pause", colour=t.active),"pause")
add(r.glyph("stop"),"stop")
add(r.glyph("next"),"next")
# Row 2
add(r.glyph("prev"),"prev")
add(r.glyph("ffwd", caption="+5s"),"ffwd")
add(r.glyph("rew", caption="-5s"),"rew")
add(r.glyph("ffwd", caption="Album"),"album-next")
add(r.route("Local", True, True),"route-local-active")
add(r.route("BT", False, True),"route-bt-avail")
add(r.route("AirPlay", False, False),"route-ap-unavail")
add(r.route("HDMI", False, True),"route-hdmi")
# Row 3
add(r.toast("Vol 65%"),"toast")
add(r.toast("Bluetooth"),"toast2")
add(r.label("Scan", sub="library"),"scan")
add(r.label("96k/24", sub="1411kbps"),"bitrate")
add(r.glyph("power", caption="Hold", colour=t.warn),"power")
add(r.glyph("shuffle"),"shuffle-glyph")
add(r.glyph("repeat"),"repeat-glyph")
add(r.blank(),"blank")

# contact sheet 8x4 with gaps
GAP=6; K=96
W=8*K+9*GAP; H=4*(K+18)+5*GAP
sheet=Image.new("RGB",(W,H),"#000000")
from PIL import ImageDraw, ImageFont
d=ImageDraw.Draw(sheet)
try: f=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",10)
except: f=ImageFont.load_default()
for i,(img,name) in enumerate(tiles[:32]):
    row,col=divmod(i,8)
    x=GAP+col*(K+GAP); y=GAP+row*(K+18+GAP)
    sheet.paste(img,(x,y))
    d.text((x+K//2,y+K+8),name,font=f,fill="#888888",anchor="mm")
sheet.save("/tmp/contact_sheet.png")
print(f"rendered {len(tiles)} keycaps -> /tmp/contact_sheet.png  ({W}x{H})")
print("layout slots defined:", len(layout.LAYOUT), " route slots:", layout.ROUTE_SLOTS)
# verify no layout key exceeds panel
bad=[k for k in layout.LAYOUT if k>=layout.KEY_COUNT]
print("out-of-range layout keys:", bad or "none")

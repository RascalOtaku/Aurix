# Frontier 1864: Saloon Interior (follow-up pack, ready to list; nothing posted)

Same style, palette, renderer and sprite sizes as Frontier 1864.

Rebuild everything (bpy module or `blender -b -P`):
  python build_saloon.py   -- --out frontier_saloon1.blend     # props 1-5
  python build_saloon2.py  -- --out frontier_saloon2.blend     # props 6-15
  python build_bartender.py -- --out frontier_bartender.blend  # walk 1-8, idle 11-14, wipe 21-28
  render_sprites.py (../blender) at --size 64 and 96, --dirs 8; bartender with --collection Animated
    and --frames 1-8 / 11-14 / 21-28 into Bartender / BartenderIdle / BartenderWipe
  pack_sheet.py renders96 sprites96 --colors 32 --outline (same for 64)
  python3 build_dist.py     # preview_all_96px.png, cover_630x500.png, dist/*.zip
  python saloon_shot.py -- shot_saloon_3d.png 64   # page screenshot

Props (all done, 3D + 64/96 px sprites, 8 directions):
- [x] Bar counter (paneled front, brass foot rail, spittoons, bottles and shot glasses)
- [x] Bar stool
- [x] Chair (spindle back)
- [x] Card table (felt top, a hand of cards, chips, bottle)
- [x] Upright piano with stool, sheet music and candle sconces
- [x] Back bar with mirror, bottle shelves, glasses and crown
- [x] Wall section (wainscot, chair rail, striped wallpaper), tiles along X
- [x] Floor section, 2 x 2 m planks, tiles both ways
- [x] Hanging oil lamp (chain, tin shade, glowing flame)
- [x] Wall sconce
- [x] Whiskey crate with six bottles in straw
- [x] Keg on a stand with tap and drip bucket
- [x] Square poker table (four hands, pot, coins, candle)
- [x] Overturned chair
- [x] Coat rack with hat and coat
- [x] Bartender: walk, idle and bar-wiping loop

Not done (could be a later free update): stairs section, interior swinging door frame.

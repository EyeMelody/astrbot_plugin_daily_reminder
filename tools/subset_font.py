"""Keep every source Unicode character; drop unused layout variants.

Build-only dependency: fonttools. The original Noto CJK font is available
from the repository's v0.5.5 tag. See licenses/ATTRIBUTION.txt and assets/OFL.txt.
"""

from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

root = Path(__file__).resolve().parents[1] / "assets"
with TTFont(root / "NotoSansCJKsc-Regular.otf") as font:
    characters = set(font.getBestCmap())
    options = subset.Options()
    options.layout_features = ["kern", "liga"]
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=characters)
    subsetter.subset(font)
    for record in font["name"].names:
        if record.nameID in (1, 3, 4, 6, 16):
            name = "MemoCJK-Regular" if record.nameID == 6 else "Memo CJK"
            record.string = name.encode(record.getEncoding())
    font.save(root / "MemoCJK-Regular.otf")
with TTFont(root / "MemoCJK-Regular.otf") as result:
    assert set(result.getBestCmap()) == characters
print(f"Preserved {len(characters)} Unicode characters")

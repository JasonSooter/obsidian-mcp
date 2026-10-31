# Fonts for social-image rendering

publish_site renders each place note's hero SVG to a 1200×630 PNG for link
previews. The SVGs name `'Space Grotesk'` and `'JetBrains Mono'`; with these
files present, resvg lays text out in the real fonts, so the PNG's text widths
match the SVG the site shows.

| File | Source (pinned commit) | License |
|---|---|---|
| `SpaceGrotesk-Variable.ttf` | [floriankarsten/space-grotesk@03507d0](https://github.com/floriankarsten/space-grotesk/blob/03507d024a01282884232081fc6011c09ff4e849/fonts/ttf/SpaceGrotesk%5Bwght%5D.ttf) | SIL OFL 1.1 — `OFL-SpaceGrotesk.txt` |
| `JetBrainsMono-Variable.ttf` | [JetBrains/JetBrainsMono@1937130](https://github.com/JetBrains/JetBrainsMono/blob/19371302b95d218af43299bce79ddbddd0bc364d/fonts/variable/JetBrainsMono%5Bwght%5D.ttf) | SIL OFL 1.1 — `OFL-JetBrainsMono.txt` |

Variable fonts, so every weight the SVGs use (400, 600) comes from one file.

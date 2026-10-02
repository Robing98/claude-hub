---
title: Orbis: window and art
description: Rule 1.26: the window is the canvas, art is never scaled to fit it.
load: on-demand
when: Before you edit rendering, the camera, the window size handling, or art plates.
projects: github.com/robing98/orbis
order: 50
---
### 1.26 The window is the canvas; art is never scaled to fit it

The view is measured in tiles. A wider window shows more world beside the
player and a taller one shows more above and below; nothing is letterboxed,
pillarboxed, or scaled to a 16 by 9 frame, and an ultrawide is not a mode.
Every plate is 1920 by 1080 play pixels at one density, wraps rather than
stretching, and a window larger than the plate is filled by extending the
plate's own edge. Reference windows run to 5120 by 1440. See `docs/art.md`,
"The window is the canvas".

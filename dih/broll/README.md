# B-roll library (labeled)

Drop your Dog is Human footage here, organized so the editor can find it by
what's happening on screen. Two things carry the label:

1. **Folder name** — e.g. `dog scratching/`, `cleaning ears/`, `happy dog/`.
2. **File name** — optional extra detail, e.g. `happy dog/golden-retriever-zoomies.mp4`.

The script generator is told the full list of folder labels that exist here, and
it tags each line of the voiceover with the best-fitting label. The matcher then
pulls a clip from that label's folder. If a line has no good label, it falls back
to the CLIP visual matcher (which "looks" at the actual frames).

## Rules of thumb
- One concept per folder. Keep labels short and literal ("cleaning ears", not
  "grooming routine part 2").
- More clips per label = more variety across the 8 weekly videos (the matcher
  avoids reusing the same clip twice in one video).
- Any video format works (`.mp4 .mov .mkv .webm .m4v`). Vertical or horizontal —
  clips are auto scaled + center-cropped to 1080x1920.

## Example labels (rename/add your own)
dog scratching · cleaning ears · happy dog · itchy skin · dog at vet ·
applying product · owner petting dog · dog eating · before and after · product shot

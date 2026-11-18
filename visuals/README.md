# visuals

Owner, 2026-11-17 ("I want all of these"): how the bot looks on Discord. Pure: no channel package, no database, no state.

## What is built

- **Coloured cards** (`style.py`, `KIND_STYLE`). A notification of a listed kind is sent as a coloured card when it goes to Discord anyway:
  a notice with buttons or an embed, or a plain notice for a user who reads notices on Discord. The heading (icon and name) is the message
  text, so a phone notification still says what it is; the card carries the whole text. A plain notice for a user who reads them in Chaster
  chat stays plain there: a card never moves a message to another channel. Unknown kinds and texts over 4,000 characters stay plain.
  A decided outcome is coloured by what it is (declined red, approved or verified green), whatever its kind.
- **Task cards** (`cards.py`, type `task`). An offer (a notice with buttons) of an obedience task, temptation, board, recovery task or
  training gets a picture: its name and its reward when it has exactly one.
- **The weekly card** (type `week`, built by `weekly_review.service.week_card`): the seven days (clean, missed, vacation, not counted yet)
  and tiles for check-ins, commitments, spot checks, lines and tokens. Only what the text of the review says too.
- **Progress bars** (`bar`): the Penalty Window status (`status`) shows the time served as a bar, from the same numbers it already gave.
- **Not the bot's profile.** Its Discord avatar and banner are the owner's own design; code never sets them (owner, 2026-11-18: an avatar
  and banner drawn here replaced his once and were taken out).

Pictures are drawn with Pillow at delivery, on this PC. A picture that cannot be drawn is left out; the message still goes.

## Never drawn

The Trust score or level, the token pool, and anything about when the release comes or how close it is (philosophy 2.6,
release_engine/README.md). That is why there is no bar toward the next token or toward a release, and none for supervision, whose end
follows the Trust score.

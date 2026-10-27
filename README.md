# yt.py

Single-file CLI for managing YouTube Music playlists through [ytmusicapi](https://github.com/sigma67/ytmusicapi). Authentication comes from your browser's cookies — no API key, no OAuth setup.

## Install

```sh
uv sync
uv run yt.py --help
```

Deps: `ytmusicapi`, `browser-cookie3`, `requests` (Python ≥ 3.13).

## Auth

Cookies are read straight out of the browser's cookie database and cached in `$XDG_STATE_HOME/ytm/auth.json` (mode `0600`, TTL 15 min). The `SAPISIDHASH` authorization header is regenerated on every run, so only the cookies matter.

```sh
uv run yt.py auth                  # auto-detect the browser
uv run yt.py auth -b firefox
uv run yt.py show -b chromium
uv run yt.py auth --cookie-file ~/cookies.sqlite
```

`-b/--browser` and `--cookie-file` are global, so any command can pin its cookie source. Supported: `firefox`, `chrome`, `chromium`, `brave`, `edge`, `vivaldi`, `opera`, `opera_gx`, and `auto` (default). Detection covers native paths, `snap` and flatpak installs; the freshest database wins. Every command extracts cookies on demand, so `auth` is optional — it just verifies the session and reports the account. Explicit `--cookie-file` reads skip the cache and never overwrite it.

Recovery is automatic: a `401`/`403` triggers one re-read of the browser cookies and a retry. If that fails, the session is genuinely dead — log into <https://music.youtube.com> again.

Chromium-family cookie decryption can fail on Linux when the keyring is unavailable. Launch the browser with `--password-store=basic` or pass `--cookie-file`. Chrome's App-Bound Encryption (`v20`) is unsupported by `browser-cookie3`.

## Commands

`-p/--playlist` selects the target playlist (id or URL) and is required for every command that touches one, unless `YTM_PLAYLIST` is set.

| Command | Purpose |
| --- | --- |
| `auth` | extract cookies, verify the session, print the account |
| `playlists` | list library playlists |
| `show` | list tracks with 1-based positions |
| `search` | search songs, videos, albums, artists, playlists |
| `add` | add tracks by query or videoId, skipping duplicates |
| `remove` | remove by position, range, or videoId |
| `move` | reorder to a position or another track's slot |

Positions are authoritative everywhere: they match what YouTube Music shows, and the surrounding toolchain assumes track tags equal positions.

```sh
uv run yt.py show -p PLxxxxxxxxxxxx
uv run yt.py -p PLxxxxxxxxxxxx show -n 20
uv run yt.py search "nightcore rather be" -f videos -n 5
uv run yt.py -p PLxxxxxxxxxxxx add "nightcore aurelle" "nightcore let you go" --dry
uv run yt.py -p PLxxxxxxxxxxxx add dQw4w9WgXcQ --id
uv run yt.py -p PLxxxxxxxxxxxx remove 12 15-18 --dry
uv run yt.py -p PLxxxxxxxxxxxx remove 12 15-18 --yes
uv run yt.py -p PLxxxxxxxxxxxx move 85 84 --dry
uv run yt.py -p PLxxxxxxxxxxxx move 85 90 --yes
uv run yt.py -p PLxxxxxxxxxxxx move 6BcguYMzytA tyaPqW0bO5M --yes
```

### Semantics

- `add` resolves each query with `filter=songs` and uses the top result. videoIds already in the playlist are skipped (`~` in human output, `skipped` in JSON) unless `--dupes`. Adds batch at 50 per request.
- `remove` accepts `3`, `15-18`, or a videoId, in any mix.
- `move SRC DST` is remove-then-insert, like `list.insert`: `yt move 5 3` leaves track 5 at position 3 and shifts everything between. A numeric `DST` is the final 1-based position; a videoId `DST` means "take that track's slot" (insert before it). `SRC` may be a position or a videoId, and moving a track onto itself reports `already in place`.
- `remove` and `move` refuse to run without `--yes` when stdin is not a TTY; on a TTY they prompt. `--dry` never mutates.
- Mutations require edit rights: a session that is merely reading a public playlist gets exit code `5` with a read-only message instead of a server error.
- Mutations are verified: the tool re-fetches the playlist and polls until the change is visible. Exit code `6` plus `"verified": false` means the call succeeded but has not settled — re-check with `show` before doing anything position-dependent. YouTube applies `edit_playlist` asynchronously; never chain moves off a stale snapshot.

## JSON

`-j`/`--json` works before or after the subcommand. Exactly one JSON document is written to stdout — for failures too — so agents never have to parse prose. Diagnostics and prompts go to stderr.

```json
{"ok":true,"cmd":"show","playlist":{"id":"PLxxxxxxxxxxxx","title":"My Playlist","count":2,"duration":"7+ hours","description":null,"editable":true},"count":1,"tracks":[{"position":1,"videoId":"dQw4w9WgXcQ","setVideoId":"0000000000000000","title":"Track Title","artists":["Artist"],"album":null,"duration":"2:06","duration_seconds":126}]}
```

`editable` is `false` when the session cannot modify the playlist; every track then carries `"setVideoId": null` and human output is marked `(read-only)`.

```json
{"ok":false,"cmd":"remove","error":{"code":"conflict","message":"refusing to modify the playlist without --yes","hint":"add --yes to confirm, or --dry to preview"}}
```

Every document carries `ok` and `cmd`. Mutating commands add `dry`, `verified`, and their own arrays (`add`/`skipped`, `removed`, `from`/`to`).

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | internal error |
| 2 | usage error |
| 3 | auth missing, stale, or refused |
| 4 | playlist/track/query not found |
| 5 | refused without `--yes` |
| 6 | change sent but not yet observed |
| 7 | network failure |
| 8 | API rejection |

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `YTM_PLAYLIST` | unset | target playlist when `-p` is not given |
| `YTM_AUTH_TTL` | `900` | seconds before cached cookies are re-read |
| `XDG_STATE_HOME` | `~/.local/state` | where `ytm/auth.json` lives |

## Library mirror

The playlist is mirrored in the VPS music library via [mimport](https://github.com/OfficialCracky/mimport): each track's `SOURCEURL` tag holds the full YouTube URL and `track` holds the playlist position.

Inserting or removing mid-order requires renumbering all downstream library rows (`mimport library edit <id> --rename track=N`, highest position first) followed by a Navidrome full scan. Keep playlist and library in lockstep: 1 playlist track ↔ 1 library row, joined by `source_url` ↔ `videoId`.

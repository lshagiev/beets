from __future__ import annotations

import os
import time
from bisect import bisect_right
from typing import TYPE_CHECKING, NamedTuple

import pylast
from pylast import _extract, _number

from beets import config, plugins, ui
from beets.dbcore import types
from beets.exceptions import UserError

if TYPE_CHECKING:
    import optparse

    from beets.library import Library
    from beets.logging import BeetsLogger as Logger

    from ._utils.playcount import Track

API_URL = "https://ws.audioscrobbler.com/2.0/"

# PATCH: file that remembers the last finished page, next to library.db
PROGRESS_FILE = "lastimport_progress.txt"
# PATCH: time of the last completed import, used by --recent
LAST_RUN_FILE = "lastimport_last_run.txt"


class OurTrack(pylast.Track):
    mbid: str


class OurTopItem(NamedTuple):
    item: OurTrack
    weight: float


class LastImportPlugin(plugins.BeetsPlugin):
    def __init__(self) -> None:
        super().__init__()
        config["lastfm"].add({"user": "", "api_key": plugins.LASTFM_KEY})
        config["lastfm"]["user"].redact = True
        config["lastfm"]["api_key"].redact = True
        # PATCH: error_retries / error_wait control retrying on API errors
        self.config.add(
            {
                "per_page": 500,
                "retry_limit": 3,
                "error_retries": 5,
                "error_wait": 30,
            }
        )
        self.item_types = {"lastfm_play_count": types.INTEGER}

    def commands(self) -> list[ui.Subcommand]:
        cmd = ui.Subcommand("lastimport", help="import last.fm play-count")
        # PATCH: options to choose where to start
        cmd.parser.add_option(
            "--restart",
            action="store_true",
            default=False,
            help="ignore saved progress and start from page 1",
        )
        cmd.parser.add_option(
            "--start-page",
            type="int",
            default=None,
            help="start from this page number",
        )
        cmd.parser.add_option(
            "--recent",
            action="store_true",
            default=False,
            help="only add scrobbles made since the last completed import",
        )
        cmd.parser.add_option(
            "--since",
            default=None,
            metavar="YYYY-MM-DD",
            help="with --recent: add scrobbles made since this date",
        )

        def func(lib: Library, opts: optparse.Values, args: list[str]) -> None:
            if opts.recent or opts.since:
                import_recent(lib, self._log, opts.since)
            else:
                import_lastfm(lib, self._log, opts.restart, opts.start_page)

        cmd.func = func
        return [cmd]


class CustomUser(pylast.User):
    """Custom user class derived from pylast.User, and overriding the
    _get_things method to return MBID and album. Also introduces new
    get_top_tracks_by_page method to allow access to more than one page of top
    tracks.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def _get_things(
        self,
        method: str,
        thing_type: type[pylast.Track | pylast.Album],
        params: type[pylast._Opus] | None = None,
        cacheable: bool = True,
        stream: bool = False,
    ) -> tuple[list[OurTopItem], int]:
        """Returns a list of the most played thing_types by this thing, in a
        tuple with the total number of pages of results. Includes an MBID, if
        found.
        """
        doc = self._request(f"{self.ws_prefix}.{method}", cacheable, params)

        toptracks_node = doc.getElementsByTagName("toptracks")[0]
        total_pages = int(toptracks_node.getAttribute("totalPages"))

        seq = []
        for node in doc.getElementsByTagName(thing_type.__name__.lower()):
            title = _extract(node, "name")
            artist = _extract(node, "name", 1)
            mbid = _extract(node, "mbid")
            playcount = _number(_extract(node, "playcount"))

            thing = OurTrack(artist, title, self.network)
            thing.mbid = mbid  # type: ignore[union-attr]
            seq.append(OurTopItem(thing, playcount))

        return seq, total_pages

    def get_top_tracks_by_page(
        self,
        period: str = pylast.PERIOD_OVERALL,
        limit: int | None = None,
        page: int = 1,
        cacheable: bool = True,
    ) -> tuple[list[OurTopItem], int]:
        """Returns the top tracks played by a user, in a tuple with the total
        number of pages of results.
        * period: The period of time. Possible values:
          o PERIOD_OVERALL
          o PERIOD_7DAYS
          o PERIOD_1MONTH
          o PERIOD_3MONTHS
          o PERIOD_6MONTHS
          o PERIOD_12MONTHS
        """

        params = self._get_params()
        params["period"] = period
        params["page"] = page
        if limit:
            params["limit"] = limit

        return self._get_things("getTopTracks", pylast.Track, params, cacheable)


# PATCH: in-memory library index. The stock matcher runs up to two full
# SQL table scans (exact, then substring LIKE) per Last.fm track, i.e.
# ~1000 scans per page. This loads the needed columns once and matches
# with the same rules in memory.
class LibraryIndex:
    FIELD = "lastfm_play_count"

    def __init__(self, lib: Library) -> None:
        self.lib = lib
        with lib.transaction() as tx:
            rows = tx.query(
                "SELECT id, title, artist, album, mb_trackid FROM items"
            )
            counts = tx.query(
                "SELECT entity_id, value FROM item_attributes WHERE key = ?",
                (self.FIELD,),
            )
        self.ids: list[int] = []
        self.artists: list[str] = []
        self.albums: list[str] = []
        self.by_title: dict[str, list[int]] = {}
        self.by_mbid: dict[str, list[int]] = {}
        titles = []
        for n, row in enumerate(rows):
            title = _low(row[1])
            self.ids.append(row[0])
            self.artists.append(_low(row[2]))
            self.albums.append(_low(row[3]))
            titles.append(title)
            self.by_title.setdefault(title, []).append(n)
            if row[4]:
                self.by_mbid.setdefault(row[4], []).append(n)
        # All titles in one string so substring search runs in C.
        self.starts: list[int] = []
        pos = 0
        for title in titles:
            self.starts.append(pos)
            pos += len(title) + 1
        self.blob = "\n".join(titles)
        self.counts = {int(e): str(v) for e, v in counts}

    def _title_contains(self, needle: str) -> list[int]:
        found = []
        pos = self.blob.find(needle)
        while pos != -1:
            n = bisect_right(self.starts, pos) - 1
            found.append(n)
            if n + 1 >= len(self.starts):
                break
            pos = self.blob.find(needle, self.starts[n + 1])
        return found

    def match(self, track: Track) -> list[int]:
        mbid = track["mbid"]
        if mbid and mbid in self.by_mbid:
            return [self.ids[n] for n in self.by_mbid[mbid]]

        title = _low(track["name"])
        artist = _low(track["artist"])
        album = _low(track.get("album") or "")
        variants = {title, title.replace("'", "’")}

        def pick(rows, ok):
            seen = []
            for n in rows:
                if n not in seen and ok(n):
                    seen.append(n)
            return [self.ids[n] for n in seen]

        exact = [n for v in variants for n in self.by_title.get(v, ())]
        hits = pick(
            exact,
            lambda n: self.artists[n] == artist
            or (album and self.albums[n] == album),
        )
        if hits:
            return hits

        sub = [n for v in variants for n in self._title_contains(v)]
        return pick(
            sub,
            lambda n: artist in self.artists[n]
            or (album and album in self.albums[n]),
        )

    def update(
        self, tracks: list[Track], log: Logger, add: bool = False
    ) -> tuple[int, int]:
        """Set play counts, or with add=True add them to the stored ones."""
        total = len(tracks)
        found_n = 0
        log.info("Received {} tracks in this page, processing...", total)
        with self.lib.transaction():
            for i, track in enumerate(tracks, 1):
                if i % 250 == 0:
                    log.info("Processing track {}/{} ...", i, total)
                ids = self.match(track)
                if not ids:
                    continue
                found_n += 1
                for item_id in ids:
                    value = track["playcount"]
                    if add:
                        value += int(self.counts.get(item_id) or 0)
                    new = str(value)
                    if self.counts.get(item_id) == new:
                        continue  # unchanged: skip the write
                    item = self.lib.get_item(item_id)
                    if item is None:
                        continue
                    item[self.FIELD] = value
                    item.store()
                    self.counts[item_id] = new
        unknown = total - found_n
        if unknown:
            log.info(
                "Acquired {}/{} play-counts ({} unknown)",
                found_n,
                total,
                unknown,
            )
        return found_n, unknown


def _low(s: object) -> str:
    # SQLite LIKE (used by the stock matcher) is case-insensitive.
    # Newlines would break the joined title string.
    return str(s or "").lower().replace("\n", " ")


# PATCH: progress-file helpers
def _progress_path() -> str:
    return os.path.join(config.config_dir(), PROGRESS_FILE)


def _read_progress() -> int:
    try:
        with open(_progress_path(), encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _write_progress(page: int) -> None:
    try:
        with open(_progress_path(), "w", encoding="ascii") as f:
            f.write(str(page))
    except OSError:
        pass


def _clear_progress() -> None:
    try:
        os.remove(_progress_path())
    except OSError:
        pass


def _last_run_path() -> str:
    return os.path.join(config.config_dir(), LAST_RUN_FILE)


def _read_last_run() -> int:
    try:
        with open(_last_run_path(), encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _write_last_run(ts: int) -> None:
    try:
        with open(_last_run_path(), "w", encoding="ascii") as f:
            f.write(str(ts))
    except OSError:
        pass


# PATCH: call Last.fm, retrying on API / network errors
def _with_retry(fn, what: str, hint: str, log: Logger):
    attempts = config["lastimport"]["error_retries"].get(int)
    wait = config["lastimport"]["error_wait"].get(int)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (
            pylast.WSError,
            pylast.NetworkError,
            pylast.MalformedResponseError,
        ) as exc:
            if attempt == attempts:
                log.error(
                    "{} still failing after {} tries: {}. {}",
                    what,
                    attempts,
                    exc,
                    hint,
                )
                raise UserError(f"Last.fm error: {what}") from exc
            delay = wait * attempt
            log.warning(
                "{} failed ({}). Retry {}/{} in {}s...",
                what,
                exc,
                attempt,
                attempts - 1,
                delay,
            )
            time.sleep(delay)
    raise UserError("unreachable")


def fetch_tracks_retry(
    user: str, page: int, limit: int, log: Logger
) -> tuple[list[Track], int]:
    return _with_retry(
        lambda: fetch_tracks(user, page, limit),
        f"Page #{page}",
        "Run 'beet lastimport' again to resume from here.",
        log,
    )


# PATCH: --recent: add only scrobbles made since the last completed run
def import_recent(lib: Library, log: Logger, since: str | None) -> None:
    user = config["lastfm"]["user"].as_str()
    if not user:
        raise UserError("You must specify a user name for lastimport")

    if since:
        try:
            ts = int(time.mktime(time.strptime(since, "%Y-%m-%d")))
        except ValueError:
            raise UserError("--since must look like 2026-11-01")
    else:
        ts = _read_last_run()
        if not ts:
            raise UserError(
                "No completed 'beet lastimport' recorded yet. Run a full "
                "'beet lastimport' once, or use --since YYYY-MM-DD."
            )

    started = int(time.time())
    log.info(
        "Fetching scrobbles for @{} since {}",
        user,
        time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
    )
    network = pylast.LastFMNetwork(api_key=config["lastfm"]["api_key"].get(str))
    played = _with_retry(
        lambda: pylast.User(user, network).get_recent_tracks(
            limit=None, time_from=ts + 1, cacheable=False
        ),
        "Fetching recent scrobbles",
        "Nothing was changed; run it again.",
        log,
    )

    # Count scrobbles per (artist, title); same matching as the full import.
    counts: dict[tuple[str, str], int] = {}
    for p in played:
        artist = p.track.artist.name if p.track.artist else ""
        key = ((artist or "").strip(), (p.track.title or "").strip())
        counts[key] = counts.get(key, 0) + 1
    log.info("{} scrobbles, {} distinct tracks", len(played), len(counts))

    if counts:
        tracks: list[Track] = [
            {"mbid": "", "artist": a, "name": t, "playcount": n}
            for (a, t), n in counts.items()
        ]
        found, unknown = LibraryIndex(lib).update(tracks, log, add=True)
        log.info("{} tracks updated, {} not in library", found, unknown)

    _write_last_run(started)
    log.info("... done!")


def import_lastfm(
    lib: Library,
    log: Logger,
    restart: bool = False,
    start_page: int | None = None,
) -> None:
    user = config["lastfm"]["user"].as_str()
    per_page = config["lastimport"]["per_page"].get(int)

    if not user:
        raise UserError("You must specify a user name for lastimport")

    log.info("Fetching last.fm library for @{}", user)
    started = int(time.time())

    page_total = 1
    page_current = 0
    # PATCH: decide where to start
    if start_page:
        page_current = max(start_page - 1, 0)
    elif restart:
        _clear_progress()
    else:
        page_current = _read_progress()
    if page_current:
        log.info(
            "Resuming after page #{} (use --restart to start over)",
            page_current,
        )
        page_total = page_current + 1

    t0 = time.time()
    index = LibraryIndex(lib)
    log.info(
        "Indexed {} library tracks in {:.1f}s", len(index.ids), time.time() - t0
    )

    found_total = 0
    unknown_total = 0
    retry_limit = config["lastimport"]["retry_limit"].get(int)
    # Iterate through a yet to be known page total count
    while page_current < page_total:
        log.info(
            "Querying page #{}{}...",
            page_current + 1,
            f"/{page_total}" if page_total > 1 else "",
        )

        for retry in range(retry_limit):
            tracks, page_total = fetch_tracks_retry(
                user, page_current + 1, per_page, log
            )
            if page_total < 1:
                # It means nothing to us!
                raise UserError("Last.fm reported no data.")

            if tracks:
                found, unknown = index.update(tracks, log)
                found_total += found
                unknown_total += unknown
                break
            log.error("ERROR: unable to read page #{}", page_current + 1)
            if retry < retry_limit:
                log.info(
                    "Retrying page #{}... ({}/{} retry)",
                    page_current + 1,
                    retry + 1,
                    retry_limit,
                )
            else:
                log.error(
                    "FAIL: unable to fetch page #{}, ",
                    "tried {} times",
                    page_current,
                    retry + 1,
                )
        page_current += 1
        _write_progress(page_current)  # PATCH: remember finished page

    _clear_progress()  # PATCH: finished, next run starts fresh
    _write_last_run(started)  # PATCH: --recent picks up from here
    log.info("... done!")
    log.info("finished processing {} song pages", page_total)
    log.info("{} unknown play-counts", unknown_total)
    log.info("{} play-counts imported", found_total)


def fetch_tracks(user: str, page: int, limit: int) -> tuple[list[Track], int]:
    network = pylast.LastFMNetwork(api_key=config["lastfm"]["api_key"].get(str))
    user_obj = CustomUser(user, network)
    results, total_pages = user_obj.get_top_tracks_by_page(
        limit=limit, page=page
    )
    return [
        {
            "mbid": t.item.mbid or "",
            "artist": (
                n.strip() if ((a := t.item.artist) and (n := a.name)) else ""
            ),
            "name": ti.strip() if ((i := t.item) and (ti := i.title)) else "",
            "playcount": int(t.weight),
        }
        for t in results
    ], total_pages

# Copyright (C) 2015 Ariel George
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; version 2.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

"""Adds bandcamp album search support to the autotagger. Requires the
BeautifulSoup library.
"""

from __future__ import (absolute_import, division, print_function, unicode_literals)

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

import beets
import beets.ui
import requests
import six
from beets import plugins
from beets.autotag.hooks import AlbumInfo, Distance, TrackInfo
from bs4 import BeautifulSoup, element

from beetsplug import fetchart


USER_AGENT = 'beets/{0} +http://beets.radbox.org/'.format(beets.__version__)
BANDCAMP_SEARCH = 'http://bandcamp.com/search?q={query}&page={page}'
BANDCAMP_ALBUM = 'album'
BANDCAMP_ARTIST = 'band'
BANDCAMP_TRACK = 'track'
ARTIST_TITLE_DELIMITER = ' - '
HTML_ID_TRACKS = 'track_table'
HTML_CLASS_TRACK = 'track_row_view'
HTML_META_DATE_FORMAT = '%d %B %Y'

WORLDWIDE = 'XW'
DIGITAL_MEDIA = 'Digital Media'
BANDCAMP = 'bandcamp'

INDEX_TITLE_PAT = r'(\d\d?. ?)([ABCDEFGH]\d\d?. )?(.*)'
SINGLE_TRACK_DURATION_PAT = r'duration":"P([^"]+)"'


def _split_artist_title(title: str) -> Tuple[Optional[str], str]:
    """Returns artist and title by splitting title on ARTIST_TITLE_DELIMITER.
    """
    parts = title.split(ARTIST_TITLE_DELIMITER, maxsplit=1)
    if len(parts) == 1:
        return None, title
    return parts[0], parts[1]


def _parse_metadata(html, url):
    # type: (BeautifulSoup, str) -> Dict[str, Union[str, int, datetime]]
    """Obtain release metadata from a page. Common to tracks and albums."""
    meta = html.find('meta')['content']  # contains all we need really
    meta_lines = [line for line in meta.splitlines() if line]

    # <release-name> by <artist>, released <day-of-the-month> <month-name> <year>
    data = next(filter(lambda x: " by " in x and ", released " in x, meta_lines))
    album, artist_and_date = data.split(" by ")
    artist, human_date = artist_and_date.split(", released ")

    # Only MusicBrainz album id is accepted as album_id - having a url here breaks
    # beets further down the process, therefore we use -1, - their default value
    return {
        "album": album,
        "album_id": url,
        "artist_url": url.split('/album/')[0],
        "artist": artist,
        "date": datetime.strptime(human_date, HTML_META_DATE_FORMAT)
    }


def _duration_in_seconds(time_str: str) -> float:
    t = datetime.strptime(time_str, "%HH%MM%SS")
    return timedelta(hours=t.hour, minutes=t.minute, seconds=t.second).total_seconds()


def _duration_from_track_html(parsed_duration: str) -> Optional[float]:
    """Get duration that's found in a page with multiple songs."""
    split_duration = parsed_duration.split(":")
    hours = "00"
    if len(split_duration) == 3:
        hours = split_duration[0]
        split_duration.remove(hours)
    minutes, seconds = split_duration
    return _duration_in_seconds(f'{hours}H{minutes}M{seconds}S')


def _duration_from_soup(soup: BeautifulSoup) -> Optional[float]:
    """Get duration that's found in a page with a single song/release."""
    match = re.search(SINGLE_TRACK_DURATION_PAT, str(soup))
    if not match:
        return None
    return _duration_in_seconds(list(match.groups()).pop())


def _trackinfo_from_meta(meta: dict, html: BeautifulSoup) -> TrackInfo:
    """Make TrackInfo object using common metadata. Additionally parse the
    duration given the html soup.
    """
    return TrackInfo(
        meta['album'],
        meta['artist_url'],
        length=_duration_from_soup(html),
        artist=meta['artist'],
        artist_id=meta['artist_url'],
        data_source=BANDCAMP,
        media=DIGITAL_MEDIA,
        data_url=meta['artist_url'],
    )


def _parse_index_with_title(string):
    # type: (str) -> Tuple[Optional[str], Optional[str], Optional[str]]
    """Examples:
        6. A2. Cool Artist - Cool Track
        3. Okay Artist - Not Bad Track
        10.Uncool_Artist - Bad Track
    """
    def clean_index(idx: str) -> str:
        """Remove . from the index and strip it."""
        return idx.replace(".", "").strip()

    match = re.match(INDEX_TITLE_PAT, string)
    if not match:
        return None, None, None

    split_match = list(match.groups())
    title = split_match.pop()
    index = clean_index(split_match[0])
    medium_index = clean_index(split_match[1]) if split_match[1] else None
    return title, index, medium_index


def _quick_track_data(info_strings):
    # type: (List[str]) -> Dict[str, Union[None, str, int, float]]
    """Parse track data from the initially parsed soup text."""
    index_title, duration = info_strings
    title, index, medium_index = _parse_index_with_title(index_title)
    return {
        "title": title,
        "index": index,
        "medium_index": medium_index,
        "duration": duration,
    }


def _volatile_track_data(track_html):
    # type: (element.Tag) -> Dict[str, Union[None, str, int, float]]
    """Given the above isn't available, try querying the html attributes."""
    return {
        "title": track_html.find(class_='track-title').text,
        "index": track_html.find(class_='track_number').text.replace(".", ""),
        "medium_index": 0,
        "duration": track_html.find(class_='time').text.replace("\n", "").replace(" ", "")
    }


class BandcampPlugin(plugins.BeetsPlugin):
    def __init__(self) -> None:
        super(BandcampPlugin, self).__init__()
        self.config.add({
            'source_weight': 0.5,
            'min_candidates': 5,
            'lyrics': False,
            'art': False,
            'split_artist_title': False
        })
        self.import_stages = [self.imported]
        self.register_listener('pluginload', self.loaded)

    def _report(self, msg: str, e: Exception, url: Optional[str] = None) -> None:
        self._log.debug(f"{msg} {url!r}: {e}")

    def loaded(self) -> None:
        # Add our own artsource to the fetchart plugin.
        # FIXME: This is ugly, but i didn't find another way to extend fetchart
        # without declaring a new plugin.
        if self.config['art']:
            for plugin in plugins.find_plugins():
                if isinstance(plugin, fetchart.FetchArtPlugin):
                    plugin.sources = [
                        BandcampAlbumArt(plugin._log, self.config)
                    ] + plugin.sources
                    fetchart.ART_SOURCES['bandcamp'] = BandcampAlbumArt
                    fetchart.SOURCE_NAMES[BandcampAlbumArt] = 'bandcamp'
                    break

    def album_distance(self, items, album_info, _):
        # type: (List[TrackInfo], AlbumInfo, Any) -> Distance
        """Returns the album distance.
        """
        dist = Distance()
        if hasattr(album_info, 'data_source') and album_info.data_source == 'bandcamp':
            dist.add('source', self.config['source_weight'].as_number())
        return dist

    def candidates(self, items, artist, album, va_likely, extra_tags=None):
        # type: (List[TrackInfo], str, str, bool, Optional[List[str]]) -> List[AlbumInfo]
        """Returns a list of AlbumInfo objects for bandcamp search results
        matching an album and artist (if not various).
        """
        return self.get_albums(album)

    def album_for_id(self, album_id: str) -> AlbumInfo:
        """Fetches an album by its bandcamp ID and returns an AlbumInfo object
        or None if the album is not found.
        """
        # We use album url as id, so we just need to fetch and parse the album page.
        return self.get_album_info(album_id)

    def item_candidates(self, item, artist, album):
        """Returns a list of TrackInfo objects from a bandcamp search matching
        a singleton.
        """
        param = item.title or item.album or item.artist
        if param:
            return self.get_tracks(param)
        return []

    def track_for_id(self, track_id: str) -> TrackInfo:
        """Fetches a track by its bandcamp ID and returns a TrackInfo object
        or None if the track is not found.
        """
        return self.get_track_info(track_id)

    def imported(self, session, task):
        """Import hook for fetching lyrics from bandcamp automatically.
        """
        if self.config['lyrics']:
            for item in task.imported_items():
                # Only fetch lyrics for items from bandcamp
                if hasattr(item, 'data_source') and item.data_source == 'bandcamp':
                    self.add_lyrics(item, True)

    def get_albums(self, query: str) -> List[AlbumInfo]:
        """Returns a list of AlbumInfo objects for a bandcamp search query.
        """
        albums = []
        for url in self._search(query, BANDCAMP_ALBUM):
            album = self.get_album_info(url)
            if album is not None:
                albums.append(album)
        return albums

    def _get(self, url: str) -> BeautifulSoup:
        """Returns a BeautifulSoup object with the contents of url.
        """
        # TODO: Handle the error properly
        headers = {'User-Agent': USER_AGENT}
        try:
            r = requests.get(url, headers=headers)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            self._report("Communication error while fetching album", e, url)
            return None
        return BeautifulSoup(r.text, 'html.parser')

    def get_album_info(self, url: str) -> Optional[AlbumInfo]:
        """Return an AlbumInfo object for a bandcamp album page.
        If someone's given a link to a track instead, return that track.
        """
        html = self._get(url)
        if not html:
            return None
        try:
            meta = _parse_metadata(html, url)
            tracks = []
            try:
                tracks_html = html.find(id=HTML_ID_TRACKS).find_all(
                    class_=HTML_CLASS_TRACK
                )
            except AttributeError:  # Oops, got a single track on the page
                tracks.append(_trackinfo_from_meta(meta, html))
            else:
                for row in tracks_html:
                    track = self._parse_album_track(row, url, meta['artist'])
                    tracks.append(track)
        except (ValueError, TypeError, AttributeError) as e:
            self._report("Unexpected html while scraping album", e, url)
            return None

        return AlbumInfo(
            meta['album'],
            meta['album_id'],
            meta['artist'],
            meta['artist_url'],
            tracks,
            year=meta['date'].year,
            month=meta['date'].month,
            day=meta['date'].day,
            country=WORLDWIDE,
            media=DIGITAL_MEDIA,
            data_source=BANDCAMP,
            data_url=url
        )

    def get_tracks(self, query: str) -> List[TrackInfo]:
        """Returns a list of TrackInfo objects for a bandcamp search query.
        """
        track_urls = self._search(query, BANDCAMP_TRACK)
        return [self.get_track_info(url) for url in track_urls]

    def get_track_info(self, url: str) -> Optional[TrackInfo]:
        """Returns a TrackInfo object for a bandcamp track page.
        """
        html = self._get(url)
        if not html:
            return None

        meta = _parse_metadata(html, url)
        if self.config['split_artist_title']:
            artist_from_title, meta['title'] = self._split_artist_title(meta['title'])
            if artist_from_title:
                meta['artist'] = artist_from_title

        return _trackinfo_from_meta(meta, html)

    def add_lyrics(self, item: TrackInfo, write: bool = False) -> None:
        """Fetch and store lyrics for a single item. If ``write``, then the
        lyrics will also be written to the file itself."""
        # Skip if the item already has lyrics.
        if item.lyrics:
            self._log.info('lyrics already present: {0}', item)
            return

        lyrics = self.get_item_lyrics(item)
        if not lyrics:
            self._log.info('lyrics not found: {0}', item)
            return

        self._log.info('fetched lyrics: {0}', item)
        item.lyrics = lyrics
        if write:
            item.try_write()
        item.store()

    def get_item_lyrics(self, item: TrackInfo) -> Optional[str]:
        """Get the lyrics for item from bandcamp.
        """
        # The track id is the bandcamp url when item.data_source is bandcamp.
        html = self._get(item.mb_trackid)
        if not html:
            return None
        lyrics = html.find(attrs={'class': 'lyricsText'})
        if lyrics:
            return lyrics.text
        return None

    def _search(self, query, search_type=BANDCAMP_ALBUM, page=1):
        # type: (str, str, int) -> List[str]
        """Returns a list of bandcamp urls for items of type search_type
        matching the query.
        """
        if search_type not in [BANDCAMP_ARTIST, BANDCAMP_ALBUM, BANDCAMP_TRACK]:
            self._log.debug('Invalid type for search: {0}'.format(search_type))
            return []

        urls: List[str] = []
        # Search bandcamp until min_candidates results have been found or
        # we hit the last page in the results.
        while len(urls) < self.config['min_candidates'].as_number():
            self._log.debug('Searching {}, page {}'.format(search_type, page))
            results = self._get(BANDCAMP_SEARCH.format(query=query, page=page))
            if not results:
                continue

            clazz = 'searchresult {0}'.format(search_type)
            for result in results.find_all('li', attrs={'class': clazz}):
                a = result.find(attrs={'class': 'heading'}).a
                if a:
                    urls.append(a['href'].split('?')[0])

            # Stop searching if we are on the last page.
            if not results.find('a', attrs={'class': 'next'}):
                break
            page += 1

        return urls

    def _parse_album_track(
        self, track_html: element.Tag, album_url: str, album_artist: str
    ) -> TrackInfo:
        """Returns a TrackInfo derived from the html describing a track in a
        bandcamp album page.
        """
        info = list(
            filter(
                lambda x: x and "info" not in x and "buy track" not in x,
                map(lambda x: x.strip(), track_html.text.replace("\n", "").split("  ")),
            )
        )
        if len(info) == 2:
            track = _quick_track_data(info)
            print(track)
        else:
            track = _volatile_track_data(track_html)
        length = _duration_from_track_html(track['duration'])

        artist, title = _split_artist_title(track['title'])
        if not artist:
            artist = album_artist

        track_el = track_html.find(href=re.compile('/track'))
        track_url = album_url.split("/album")[0] + track_el['href']
        return TrackInfo(
            title,
            track_url,
            index=track['index'],
            medium_index=track['medium_index'],
            length=length,
            data_url=track_url,
            artist=artist,
        )


class BandcampAlbumArt(fetchart.RemoteArtSource):
    NAME = u"Bandcamp"

    def get(self, album, plugin, paths):
        """Return the url for the cover from the bandcamp album page.
        This only returns cover art urls for bandcamp albums (by id).
        """
        field = album.mb_albumid
        if isinstance(field, six.string_types) and 'bandcamp' in field:
            try:
                headers = {'User-Agent': USER_AGENT}
                r = requests.get(field, headers=headers)
                r.raise_for_status()
                album_html = BeautifulSoup(r.text, 'html.parser').find(id='tralbumArt')
                image_url = album_html.find('a', attrs={'class': 'popupImage'})['href']
                yield self._candidate(url=image_url,
                                      match=fetchart.Candidate.MATCH_EXACT)
            except requests.exceptions.RequestException as e:
                self._log.debug("Communication error getting art for {0}: {1}"
                                .format(album, e))
            except ValueError:
                pass


class BandcampException(Exception):
    pass

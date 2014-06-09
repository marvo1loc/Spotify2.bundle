from client import SpotifyClient
from routing import function_path, route_path
from utils import localized_format, authenticated, ViewMode, Track, TrackMetadata, check_restart

from cachecontrol import CacheControl
from spotify_web.friendly import SpotifyArtist, SpotifyAlbum, SpotifyTrack
from threading import RLock, Event, Semaphore

import locale
import requests
import urllib
import time
from random import randint

class SpotifyPlugin(object):
    def __init__(self):
        self.client = None
        self.server = None
        self.play_lock      = RLock()
        self.metadata_lock  = RLock()
        self.start_lock     = Semaphore(1)
        self.start_marker   = Event()
        self.current_track  = None

        Dict.Reset()
        Dict['play_count']             = 0
        Dict['last_restart']           = 0
        Dict['play_restart_scheduled'] = False
        Dict['schedule_restart_each']  = 5*60   # restart each  X minutes
        Dict['play_restart_each']      = 2      # restart each  X plays
        Dict['play_restart_after']     = 2      # restart after X seconds when play count has been reached

        self.start()

        self.session = requests.session()
        self.session_cached = CacheControl(self.session)

        # if a restart happened, then we should't do it again
        Thread.CreateTimer(Dict['schedule_restart_each'], self.scheduled_restart, globalize=True)


    @property
    def username(self):
        return Prefs["username"]

    @property
    def password(self):
        return Prefs["password"]

    def preventive_play_restart(self):
        self.start()
        Dict['play_restart_scheduled'] = False

    def scheduled_restart(self):
        Log("Starting scheduled restart")
        
        now  = time.time()
        diff = now - Dict['last_restart']
        Log.Debug("Distance in seconds from prev restart is: %s. Difference needed: %s" % (str(diff), str(Dict['schedule_restart_each'])))
        
        # if a restart happened, then we should't do it again
        if diff >= Dict['schedule_restart_each']:
            self.start()

        # Schedule the new timer
        new_time = Dict['schedule_restart_each'] - diff if diff < Dict['schedule_restart_each'] else Dict['schedule_restart_each']
        Log.Debug("Scheduling next restart in %s seconds" % str(new_time))
        Thread.CreateTimer(new_time, self.scheduled_restart, globalize=True)

    @check_restart
    def preferences_updated(self):
        """ Called when the user updates the plugin preferences"""
        # Trigger a client restart
        self.start()

    def start(self):
        """ Start the Spotify client and HTTP server """
        if not self.username or not self.password:
            Log("Username or password not set: not logging in")
            return False

        can_start = self.start_lock.acquire(blocking=False)
        try:
            # If there is a start in process, just wait until it finishes, but don't raise another one
            if not can_start:
                Log.Debug("Start already in progress, waiting it finishes to return")
                self.start_lock.acquire()
            else:
                Log.Debug("Start triggered, entering private section")
                self.start_marker.clear()
                
                if self.client:            
                    self.client.restart(self.username, self.password)
                else:
                    self.client = SpotifyClient(self.username, self.password)

                self.current_track   = None
                Dict['play_count']   = 0
                Dict['last_restart'] = time.time()
                self.start_marker.set()
                Log.Debug("Start finished, leaving private section")
        finally:
            self.start_lock.release()

        return self.client and self.client.is_logged_in()

    @check_restart
    def play(self, uri):
        """ Play a spotify track: redirect the user to the actual stream """
        Log('play(%s)' % repr(uri))

        if not self.client.is_track_uri_valid(uri):
            Log("Play track callback invoked with invalid URI")
            return
        
        # Process play request one by one to avoid errors
        with self.play_lock:

            track_url = self.get_track_url(uri)
            
            # If first request failed, trigger re-connection to spotify
            retry_num = 0 
            while not track_url and retry_num < 3:
                Log.Info('get_track_url failed, re-connecting to spotify...')
                time.sleep(retry_num*0.5) # Wait some time based on number of failures
                if self.start():
                    track_url = self.get_track_url(uri)
                retry_num = retry_num + 1

            if track_url == False:
                Log.Error("Play track couldn't be obtained. This is very bad :-(")
                return None
            
            Dict['play_count'] = Dict['play_count'] + 1
            if Dict['play_count'] >= Dict['play_restart_each'] and not Dict['play_restart_scheduled']:
                Log.Debug('Scheduling preventive restart (%s plays)' % str(Dict['play_count']))
                Thread.CreateTimer(Dict['play_restart_after'], self.preventive_play_restart, globalize=True)
                Dict['play_restart_scheduled'] = True

            return Redirect(track_url)
    
    def get_track_url(self, track_uri):
        if not self.client.is_track_uri_valid(track_uri):
            return None

        track_url = None
        if self.current_track and self.current_track.matches(track_uri):
            Log.Debug('Cache hit for track with uri: %s' % track_uri)
            track_url = self.current_track.url
        else:
            self.current_track = None
            track = self.client.get(track_uri)
            if track:
                track_url = track.getFileURL(urlOnly=True, retries=1) #self.client.getTrackFileURL(track_uri, retries=1)
        
        return track_url

    #
    # TRACK DETAIL
    #
    @check_restart
    def metadata(self, uri): 
        """ Get a track metadata """
        Log('metadata(%s)' % repr(uri))

        if not self.client.is_track_uri_valid(uri):
            Log("Metadata callback invoked with invalid URI")
            return
  
        # Process metadata request one by one to avoid errors
        with self.metadata_lock:

            track_metadata = self.get_track_metadata(uri)
            
            # If first request failed, trigger re-connection to spotify
            retry_num = 0 
            while not track_metadata and retry_num < 3:
                Log.Info('get_track_metadata failed, re-connecting to spotify...')
                time.sleep(retry_num*0.5) # Wait some time based on number of failures
                if self.start():
                    track_metadata = self.get_track_metadata(uri)
                retry_num = retry_num + 1

            if track_metadata:
                track_object = self.create_track_object_from_metatada(track_metadata)
                oc = ObjectContainer()
                oc.add(track_object)
                return oc
            else:
                return ObjectContainer()

    def get_track_metadata(self, track_uri):
        if not self.client.is_track_uri_valid(track_uri):
            return None

        track = self.client.get(track_uri)
        if not track:
            return None

        track_uri       = track.getURI().decode("utf-8")
        title           = track.getName().decode("utf-8")
        image_url       = self.select_image(track.getAlbumCovers())
        track_duration  = int(track.getDuration())
        track_number    = int(track.getNumber())
        track_album     = track.getAlbum(nameOnly=True).decode("utf-8")
        track_artists   = track.getArtists(nameOnly=True).decode("utf-8")
        metadata        = TrackMetadata(title, image_url, track_uri, track_duration, track_number, track_album, track_artists)

        return metadata

    @staticmethod
    def select_image(images):
        if images == None:
            return None

        if images.get(640):
            return images[640]
        elif images.get(320):
            return images[320]
        elif images.get(300):
            return images[300]
        elif images.get(160):
            return images[160]
        elif images.get(60):
            return images[60]
        
        Log.Info('Unable to select image, available sizes: %s' % images.keys())
        return None

    def get_uri_image(self, uri):
        images = None
        obj = self.client.get(uri)            
        if isinstance(obj, SpotifyArtist):
            images = obj.getPortraits()
        elif isinstance(obj, SpotifyAlbum):
            images = obj.getCovers()
        elif isinstance(obj, SpotifyTrack):
            images = obj.getAlbum().getCovers()
        elif isinstance(obj, SpotifyPlaylist):
            images = obj.getImages()
        
        return self.select_image(images)

    @authenticated
    @check_restart
    def image(self, uri):
        if not uri:
            # TODO media specific placeholders
            return Redirect(R('placeholder-artist.png'))

        Log.Debug('Getting image for: %s' % uri)

        if uri.startswith('spotify:'):
            # Fetch object for spotify URI and select image
            image_url = self.get_uri_image(uri)

            if not image_url:
                # TODO media specific placeholders
                return Redirect(R('placeholder-artist.png'))
        else:
            # pre-selected image provided
            Log.Debug('Using pre-selected image URL: "%s"' % uri)
            image_url = uri

        return self.session_cached.get(image_url).content

    #
    # SECOND_LEVEL_MENU
    #

    @authenticated
    @check_restart
    def explore(self):
        """ Explore shared music
        """
        return ObjectContainer(
            objects=[
                DirectoryObject(
                    key=route_path('explore/featured_playlists'),
                    title=L("MENU_FEATURED_PLAYLISTS"),
                    thumb=R("icon-explore-featuredplaylists.png")
                ),
                DirectoryObject(
                    key=route_path('explore/top_playlists'),
                    title=L("MENU_TOP_PLAYLISTS"),
                    thumb=R("icon-explore-topplaylists.png")
                ),
                DirectoryObject(
                    key=route_path('explore/new_releases'),
                    title=L("MENU_NEW_RELEASES"),
                    thumb=R("icon-explore-newreleases.png")
                )                
            ],
        )

    @authenticated
    @check_restart
    def discover(self):
        Log("discover")

        oc = ObjectContainer(
            title2=L("MENU_DISCOVER"),
            view_group=ViewMode.Stories
        )

        stories = self.client.discover()
        for story in stories:
            self.add_story_to_directory(story, oc)
        return oc        

    @authenticated
    @check_restart
    def radio(self):
        """ Show radio options """
        return ObjectContainer(
            objects=[
                DirectoryObject(
                    key=route_path('radio/stations'),
                    title=L("MENU_RADIO_STATIONS"),
                    thumb=R("icon-radio-stations.png")
                ),
                DirectoryObject(
                    key=route_path('radio/genres'),
                    title=L("MENU_RADIO_GENRES"),
                    thumb=R("icon-radio-genres.png")
                )
            ],
        )

    @authenticated
    @check_restart
    def your_music(self):
        """ Explore your music
        """
        return ObjectContainer(
            objects=[
                DirectoryObject(
                    key=route_path('your_music/playlists'),
                    title=L("MENU_PLAYLISTS"),
                    thumb=R("icon-playlists.png")
                ),
                DirectoryObject(
                    key=route_path('your_music/starred'),
                    title=L("MENU_STARRED"),
                    thumb=R("icon-starred.png")
                ),                  
                DirectoryObject(
                    key=route_path('your_music/albums'),
                    title=L("MENU_ALBUMS"),
                    thumb=R("icon-albums.png")
                ),
                DirectoryObject(
                    key=route_path('your_music/artists'),
                    title=L("MENU_ARTISTS"),
                    thumb=R("icon-artists.png")
                ),            
            ],
        )

    #
    # EXPLORE
    #

    @authenticated
    @check_restart
    def featured_playlists(self):
        Log("featured playlists")

        oc = ObjectContainer(
            title2=L("MENU_FEATURED_PLAYLISTS"),
            content=ContainerContent.Playlists,
            view_group=ViewMode.Playlists
        )

        playlists = self.client.get_featured_playlists()

        for playlist in playlists:
            self.add_playlist_to_directory(playlist, oc)

        return oc

    @authenticated
    @check_restart
    def top_playlists(self):
        Log("top playlists")

        oc = ObjectContainer(
            title2=L("MENU_TOP_PLAYLISTS"),
            content=ContainerContent.Playlists,
            view_group=ViewMode.Playlists
        )

        playlists = self.client.get_top_playlists()

        for playlist in playlists:
            self.add_playlist_to_directory(playlist, oc)

        return oc

    @authenticated
    @check_restart
    def new_releases(self):
        Log("new releases")

        oc = ObjectContainer(
            title2=L("MENU_NEW_RELEASES"),
            content=ContainerContent.Albums,
            view_group=ViewMode.Albums
        )

        albums = self.client.get_new_releases()

        for album in albums:
            self.add_album_to_directory(album, oc)

        return oc
    
    #
    # RADIO
    #

    @authenticated
    @check_restart
    def radio_stations(self):
        Log('radio stations')
        
        oc = ObjectContainer(title2=L("MENU_RADIO_STATIONS"))
        
        stations = self.client.get_radio_stations()
        for station in stations:
            oc.add(PopupDirectoryObject(
                        key=route_path('radio/stations/' + station.getURI()),
                        title=station.getTitle(),
                        thumb=function_path('image.png', uri=self.select_image(station.getImages()))
                        ))
        return oc

    @authenticated
    @check_restart
    def radio_genres(self):
        Log('radio genres')

        oc = ObjectContainer(title2=L("MENU_RADIO_GENRES"))
        
        genres = self.client.get_radio_genres()
        for genre in genres:
            oc.add(PopupDirectoryObject(
                        key=route_path('radio/genres/' + genre.getURI()),
                        title=genre.getTitle(),
                        thumb=function_path('image.png', uri=self.select_image(genre.getImages()))
                        ))
        return oc

    @authenticated
    @check_restart
    def radio_track_num(self, uri):
        Log('radio track num')
        return ObjectContainer(
            title2=L("MENU_RADIO_TRACK_NUM"),
            objects=[
                DirectoryObject(
                    key=route_path('radio/play/' + uri + '/10'),
                    title=localized_format("MENU_TRACK_NUM", "10"),
                    thumb=R("icon-radio-item.png")
                ),
                DirectoryObject(
                    key=route_path('radio/play/' + uri + '/20'),
                    title=localized_format("MENU_TRACK_NUM", "20"),
                    thumb=R("icon-radio-item.png")
                ),
                DirectoryObject(
                    key=route_path('radio/play/' + uri + '/50'),
                    title=localized_format("MENU_TRACK_NUM", "50"),
                    thumb=R("icon-radio-item.png")
                ),
                DirectoryObject(
                    key=route_path('radio/play/' + uri + '/80'),
                    title=localized_format("MENU_TRACK_NUM", "80"),
                    thumb=R("icon-radio-item.png")
                ),
                DirectoryObject(
                    key=route_path('radio/play/' + uri + '/100'),
                    title=localized_format("MENU_TRACK_NUM", "100"),
                    thumb=R("icon-radio-item.png")
                )
            ],
        )        

    @authenticated
    @check_restart
    def radio_tracks(self, uri, num_tracks):
        Log('radio tracks')

        oc     = None
        radio  = self.client.get_radio(uri)
        tracks = radio.getTracks(int(num_tracks))

        oc = ObjectContainer(
            title2     = radio.getTitle().decode("utf-8"),
            content    = ContainerContent.Tracks,
            view_group = ViewMode.Tracks
        )
        
        for track in tracks:
            self.add_track_to_directory(track, oc)

        return oc

    #
    # YOUR_MUSIC
    #

    @authenticated
    @check_restart
    def playlists(self):
        Log("playlists")

        oc = ObjectContainer(
            title2=L("MENU_PLAYLISTS"),
            content=ContainerContent.Playlists,
            view_group=ViewMode.Playlists
        )

        playlists = self.client.get_playlists()

        for playlist in playlists:
            self.add_playlist_to_directory(playlist, oc)

        return oc

    @authenticated
    @check_restart
    def starred(self):
        """ Return a directory containing the user's starred tracks"""
        Log("starred")

        oc = ObjectContainer(
            title2=L("MENU_STARRED"),
            content=ContainerContent.Tracks,
            view_group=ViewMode.Tracks
        )

        starred = self.client.get_starred()

        for x, track in enumerate(starred.getTracks()):
            self.add_track_to_directory(track, oc, index=x)

        return oc

    @authenticated
    @check_restart
    def albums(self):
        Log("albums")

        oc = ObjectContainer(
            title2=L("MENU_ALBUMS"),
            content=ContainerContent.Albums,
            view_group=ViewMode.Albums
        )
        
        albums = self.client.get_my_albums()

        for album in albums:
            self.add_album_to_directory(album, oc)

        return oc

    @authenticated
    @check_restart
    def artists(self):
        Log("artists")

        oc = ObjectContainer(
            title2=L("MENU_ARTISTS"),
            content=ContainerContent.Artists,
            view_group=ViewMode.Artists
        )
        
        artists = self.client.get_my_artists()

        for artist in artists:
            self.add_artist_to_directory(artist, oc)

        return oc

    #
    # ARTIST DETAIL
    #

    @authenticated
    @check_restart
    def artist(self, uri):
        """ Browse an artist.

        :param uri:            The Spotify URI of the artist to browse.
        """
        artist = self.client.get(uri)
        return ObjectContainer(
            title2=artist.getName().decode("utf-8"),

            objects=[
                DirectoryObject(
                    key  = route_path('artist/%s/top_tracks' % uri),
                    title=L("MENU_TOP_TRACKS"),
                    thumb=R("icon-artist-toptracks.png")
                ),
                DirectoryObject(
                    key  = route_path('artist/%s/albums' % uri),
                    title =L("MENU_ALBUMS"),
                    thumb =R("icon-albums.png")
                ),
                DirectoryObject(
                    key=route_path('radio/stations/' + uri),
                    title =L("MENU_RADIO"),
                    thumb =R("icon-radio-custom.png")
                )
            ],
        )

    @authenticated
    @check_restart
    def artist_albums(self, uri):
        """ Browse an artist.
        :param uri:            The Spotify URI of the artist to browse.
        """
        artist = self.client.get(uri)

        oc = ObjectContainer(
            title2=artist.getName().decode("utf-8"),
            content=ContainerContent.Albums
        )

        for album in artist.getAlbums():
            self.add_album_to_directory(album, oc)

        return oc
    
    @authenticated
    @check_restart
    def artist_top_tracks(self, uri):
        """ Browse an artist.
        :param uri:            The Spotify URI of the artist to browse.
        """
        oc          = None
        artist      = self.client.get(uri)        
        top_tracks  = artist.getTracks()
        
        if top_tracks:
            oc = ObjectContainer(
                title2=artist.getName().decode("utf-8"),
                content=ContainerContent.Tracks,
                view_group=ViewMode.Tracks
            )
            for track in artist.getTracks():
                self.add_track_to_directory(track, oc)
        else:
            oc = MessageContainer(
                header=L("MSG_TITLE_NO_RESULTS"),
                message=localized_format("MSG_FMT_NO_RESULTS", artist.getName().decode("utf-8"))
            )
        return oc

    #
    # ALBUM DETAIL
    #

    @authenticated
    @check_restart
    def album(self, uri):
        """ Browse an album.

        :param uri:            The Spotify URI of the album to browse.
        """
        album = self.client.get(uri)

        oc = ObjectContainer(
            title2=album.getName().decode("utf-8"),
            content=ContainerContent.Tracks,
            view_group=ViewMode.Tracks
        )

        for track in album.getTracks():
            self.add_track_to_directory(track, oc)

        return oc

    #
    # PLAYLIST DETAIL
    #

    @authenticated
    @check_restart
    def playlist(self, uri):
        pl = self.client.get(uri)

        if pl is None:
            # Unable to find playlist
            return MessageContainer(
                header=L("MSG_TITLE_UNKNOWN_PLAYLIST"),
                message='URI: %s' % uri
            )

        Log("Get playlist: %s", pl.getName().decode("utf-8"))
        Log.Debug('playlist truncated: %s', pl.obj.contents.truncated)

        oc = ObjectContainer(
            title2=pl.getName().decode("utf-8"),
            content=ContainerContent.Tracks,
            view_group=ViewMode.Tracks,
            mixed_parents=True
        )

        for x, track in enumerate(pl.getTracks()):
            self.add_track_to_directory(track, oc, index=x)

        return oc

    #
    # MAIN MENU
    #
    def main_menu(self):
        return ObjectContainer(
            objects=[
                InputDirectoryObject(
                    key=route_path('search'),
                    prompt=L("PROMPT_SEARCH"),
                    title=L("MENU_SEARCH"),
                    thumb=R("icon-search.png")
                ),
                DirectoryObject(
                    key=route_path('explore'),
                    title=L("MENU_EXPLORE"),
                    thumb=R("icon-explore.png")
                ),
                DirectoryObject(
                    key=route_path('discover'),
                    title=L("MENU_DISCOVER"),
                    thumb=R("icon-discover.png")
                ),
                DirectoryObject(
                    key=route_path('radio'),
                    title=L("MENU_RADIO"),
                    thumb=R("icon-radio.png")
                ),
                DirectoryObject(
                    key=route_path('your_music'),
                    title=L("MENU_YOUR_MUSIC"),
                    thumb=R("icon-yourmusic.png")
                ),
                PrefsObject(
                    title=L("MENU_PREFS"),
                    thumb=R("icon-preferences.png")
                )
            ],
        )
    
    #
    # Create objects
    #
    def create_track_object(self, track, index=None):
        if not track:
            return None

        # Get metadata info
        track_uri       = track.getURI().decode("utf-8")
        title           = track.getName().decode("utf-8")
        image_url       = self.select_image(track.getAlbumCovers())
        track_duration  = int(track.getDuration()) - 500
        track_number    = int(track.getNumber())
        track_album     = track.getAlbum(nameOnly=True).decode("utf-8")
        track_artists   = track.getArtists(nameOnly=True).decode("utf-8")
        metadata = TrackMetadata(title, image_url, track_uri, track_duration, track_number, track_album, track_artists)
                
        return self.create_track_object_from_metatada(metadata, index=index)

    def create_track_object_from_metatada(self, metadata, index=None):
        if not metadata:
            return None

        uri = metadata.uri
        rating_key = uri
        if index is not None:
            rating_key = '%s::%s' % (uri, index)

        art_num = str(randint(1,40)).rjust(2, "0")

        track_obj = TrackObject(
            items=[
                MediaObject(
                    parts=[PartObject(key=route_path('play/%s' % uri))],
                    #parts=[PartObject(key=HTTPLiveStreamURL(Callback(self.play, uri=uri, ext='mp3')))],
                    #parts = [PartObject(key = Callback(self.play, uri=uri, ext='mp3'))],
                    duration=metadata.duration,
                    container=Container.MP3,
                    audio_codec=AudioCodec.MP3,
                    audio_channels = 2
                )
            ],

            key = route_path('metadata', uri),
            rating_key = rating_key,

            title  = metadata.title,
            album  = metadata.album,
            artist = metadata.artists,

            index    = index if index != None else metadata.number,
            duration = metadata.duration,

            source_title='Spotify',
            
            art   = R('art-' + art_num + '.png'), #function_path('image.png', uri=metadata.image_url),
            thumb = function_path('image.png', uri=metadata.image_url)
        )
        Log.Debug('New track object for metadata: --|%s|%s|%s|%s|%s|%s|--' % (metadata.image_url, metadata.uri, str(metadata.duration), str(metadata.number), metadata.album, metadata.artists))

        return track_obj

    def create_album_object(self, album, custom_summary=None, custom_image_url=None):
        """ Factory method for album objects """
        title = album.getName().decode("utf-8")
        if Prefs["displayAlbumYear"] and album.getYear() != 0:
            title = "%s (%s)" % (title, album.getYear())
        artist_name = album.getArtists(nameOnly=True).decode("utf-8")
        summary     = '' if custom_summary == None else custom_summary.decode('utf-8')
        image_url   = self.select_image(album.getCovers()) if custom_image_url == None else custom_image_url

        return AlbumObject(
            key=route_path('album', album.getURI().decode("utf-8")),
            rating_key=album.getURI().decode("utf-8"),

            title=title,
            artist=artist_name,
            summary=summary,

            track_count=album.getNumTracks(),
            source_title='Spotify',

            art=function_path('image.png', uri=image_url),
            thumb=function_path('image.png', uri=image_url),
        )
    
    def create_playlist_object(self, playlist):
        uri         = urllib.quote_plus(playlist.getURI().encode('utf8')).replace("%3A", ":").decode("utf-8")
        image_url   = self.select_image(playlist.getImages())
        artist      = playlist.getUsername().decode("utf-8")
        title       = playlist.getName().decode("utf-8")
        summary     = ''
        if playlist.getDescription() != None and len(playlist.getDescription()) > 0:
            summary = playlist.getDescription().decode("utf-8")

        return AlbumObject(
            key=route_path('playlist', uri),
            rating_key=uri,
            
            title=title,
            artist=artist,
            summary=summary,

            source_title='Spotify',
            
            art=function_path('image.png', uri=image_url) if image_url != None else R("placeholder-playlist.png"),
            thumb=function_path('image.png', uri=image_url) if image_url != None else R("placeholder-playlist.png")
        )

    def create_artist_object(self, artist, custom_summary=None, custom_image_url=None):
        image_url   = self.select_image(artist.getPortraits()) if custom_image_url == None else custom_image_url
        artist_name = artist.getName().decode("utf-8")
        summary     = '' if custom_summary == None else custom_summary.decode('utf-8')

        return ArtistObject(
                key=route_path('artist', artist.getURI().decode("utf-8")),
                rating_key=artist.getURI().decode("utf-8"),

                title=artist_name,
                summary=summary,
                source_title='Spotify',

                art=function_path('image.png', uri=image_url),
                thumb=function_path('image.png', uri=image_url)
            )            


    #
    # Insert objects into container
    #

    def add_section_header(self, title, oc):
        oc.add(
            DirectoryObject(
                key='',
                title=title
            )
        )

    def add_track_to_directory(self, track, oc, index = None):
        if not self.client.is_track_playable(track):
            Log("Ignoring unplayable track: %s" % track.getName())
            return

        track_uri = track.getURI().decode("utf-8")
        if not self.client.is_track_uri_valid(track_uri):
            Log("Ignoring unplayable track: %s, invalid uri: %s" % (track.getName(), track_uri))
            return

        oc.add(self.create_track_object(track, index=index))

    def add_album_to_directory(self, album, oc, custom_summary=None, custom_image_url=None):
        if not self.client.is_album_playable(album):
            Log("Ignoring unplayable album: %s" % album.getName())
            return
        oc.add(self.create_album_object(album, custom_summary=custom_summary, custom_image_url=custom_image_url))

    def add_artist_to_directory(self, artist, oc, custom_summary=None, custom_image_url=None):
        oc.add(self.create_artist_object(artist, custom_summary=custom_summary, custom_image_url=custom_image_url))

    def add_playlist_to_directory(self, playlist, oc):
        oc.add(self.create_playlist_object(playlist))

    def add_story_to_directory(self, story, oc):
        content_type = story.getContentType()
        image_url    = self.select_image(story.getImages())
        item         = story.getObject()
        if content_type == 'artist':
            self.add_artist_to_directory(item, oc, custom_summary=story.getDescription(), custom_image_url=image_url)
        elif content_type == 'album':
            self.add_album_to_directory(item,  oc, custom_summary=story.getDescription(), custom_image_url=image_url)
        elif content_type == 'track':
            self.add_album_to_directory(item.getAlbum(), oc, custom_summary=story.getDescription() + " - " + item.getName(), custom_image_url=image_url)
        
        # Do not include playlists (just like official spotify client does)
        #elif content_type == 'playlist':
        #    self.add_playlist_to_directory(item, oc)

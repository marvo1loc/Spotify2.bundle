from settings import PLUGIN_ID

from spotify_web.friendly import Spotify
from spotify_web.spotify import Logging

class SpotifyClient(object):
    audio_buffer_size = 50
    user_agent = PLUGIN_ID

    def __init__(self, username, password):
        """ Initializer

        :param username:       The username to connect to spotify with.
        :param password:       The password to authenticate with.
        """

        # Hook logging
        Logging.hook(3, Log.Debug)
        Logging.hook(2, Log.Info)
        Logging.hook(1, Log.Warn)
        Logging.hook(0, Log.Error)

        self.username = username
        self.spotify = Spotify(username, password, log_level=3)

    #
    # Public methods
    #

    def is_logged_in(self):
        return self.spotify.logged_in()

    def restart(self, username, password):
        return self.spotify.restart(username, password)

    def shutdown(self):
        self.spotify.shutdown()

    def search(self, query, query_type='all', max_results=50, offset=0):
        """ Execute a search

        :param query:          A query string.
        """
        return self.spotify.search(query, query_type, max_results, offset)

    #
    # Media
    #

    def get(self, uri):
        return self.spotify.objectFromURI(uri)

    def is_album_playable(self, album):
        """ Check if an album can be played by a client or not """
        return True

    def is_track_playable(self, track):
        """ Check if a track can be played by a client or not """
        return True

    #
    # Explore
    #

    def get_featured_playlists(self):
        """ Return the featured playlists"""
        return self.spotify.getFeaturedPlaylists()

    def get_top_playlists(self):
        """ Return the top playlists"""
        return self.spotify.getTopPlaylists()

    def get_new_releases(self):
        """ Return the top playlists"""
        return self.spotify.getNewReleases()

    #
    # Playlists
    #

    def get_playlists(self):
        """ Return the user's playlists"""
        return self.spotify.getPlaylists()

    def get_starred(self):
        """ Return the user's starred tracks"""
        return self.get('spotify:user:%s:starred' % self.username)

    #
    # My Music
    #

    def get_my_albums(self):
        """ Return the user's albums"""
        return self.spotify.getMyMusic(type="albums")

    def get_my_artists(self):
        """ Return the user's artists"""
        return self.spotify.getMyMusic(type="artists")

    #
    #  Uri validation
    #
    def is_track_uri_valid(self, track_uri):
        return self.spotify.is_track_uri_valid(track_uri)

#!/usr/bin/python
# -*- coding: utf-8 -*-
import zlib
import xbmcvfs
from xbmcgui import Window
from xbmc import Monitor, sleep
from contextlib import contextmanager
from jurialmunkey.tmdate import set_timestamp
from jurialmunkey.futils import FileUtils
from jurialmunkey.futils import json_loads as data_loads
from json import dumps as data_dumps
import sqlite3


FILEUTILS = FileUtils()


DATABASE_NAME = 'database_v6'
TIME_MINUTES = 60
TIME_HOURS = 60 * TIME_MINUTES
TIME_DAYS = 24 * TIME_HOURS


class SimpleCache(object):
    '''simple stateless caching system for Kodi'''
    _exit = False
    _auto_clean_interval = 4 * TIME_HOURS
    _win = None
    _busy_tasks = []
    _database = None
    _memcache = False
    _basefolder = ''
    _fileutils = FILEUTILS
    _retries = 4
    _retry_polling = 0.1

    def __init__(self, folder=None, filename=None):
        '''Initialize our caching class'''
        folder = folder or DATABASE_NAME
        basefolder = f'{self._basefolder}{folder}'
        filename = filename or 'defaultcache.db'
        self._win = Window(10000)
        self._monitor = Monitor()
        self._db_file = self._fileutils.get_file_path(basefolder, filename, join_addon_data=basefolder == folder)
        self._sc_name = f'{folder}_{filename}_simplecache'
        self._queue = []
        self._re_use_con = True
        self._connection = None
        self.check_cleanup()
        self.kodi_log("CACHE: Initialized")

    @staticmethod
    def kodi_log(msg, level=0):
        from jurialmunkey.logger import Logger
        Logger('[script.module.jurialmunkey]\n').kodi_log(msg, level)

    def close(self):
        '''tell any tasks to stop immediately (as we can be called multithreaded) and cleanup objects'''
        self._exit = True
        # wait for all tasks to complete
        while self._busy_tasks and not self._monitor.abortRequested():
            sleep(25)
        self.kodi_log(f'CACHE: Closed {self._sc_name}', 2)

    def __del__(self):
        '''make sure close is called'''
        if self._queue:
            self.kodi_log(f'CACHE: Write {len(self._queue)} Items in Queue\n{self._sc_name}', 2)
        for i in self._queue:
            self._set_db_cache(*i)
        self._queue = []
        self.close()

    @contextmanager
    def busy_tasks(self, task_name):
        self._busy_tasks.append(task_name)
        try:
            yield
        finally:
            self._busy_tasks.remove(task_name)

    def get(self, endpoint, cur_time=None):
        '''
            get object from cache and return the results
            endpoint: the (unique) name of the cache object as reference
        '''
        cur_time = cur_time or set_timestamp(0, True)
        result = self._get_mem_cache(endpoint, cur_time)  # Try from memory first
        return result or self._get_db_cache(endpoint, cur_time)  # Fallback to checking database if not in memory

    def set(self, endpoint, data, cache_days=30):
        """ set data in cache """
        with self.busy_tasks(f'set.{endpoint}'):
            expires = set_timestamp(cache_days * TIME_DAYS, True)
            data = data_dumps(data, separators=(',', ':'))
            self._set_mem_cache(endpoint, expires, data)
            self._set_db_cache(endpoint, expires, data)

    def check_cleanup(self):
        '''check if cleanup is needed - public method, may be called by calling addon'''
        cur_time = set_timestamp(0, True)
        lastexecuted = self._win.getProperty(f'{self._sc_name}.clean.lastexecuted')
        if not lastexecuted:
            self._win.setProperty(f'{self._sc_name}.clean.lastexecuted', str(cur_time - self._auto_clean_interval + 600))
            return
        if (int(lastexecuted) + self._auto_clean_interval) < cur_time:
            self._do_cleanup()

    def _get_mem_cache(self, endpoint, cur_time):
        '''
            get cache data from memory cache
            we use window properties because we need to be stateless
        '''
        if not self._memcache:
            return

        # Check expiration time
        expr_endpoint = f'{self._sc_name}_expr_{endpoint}'
        expr_propdata = self._win.getProperty(expr_endpoint)
        if not expr_propdata or int(expr_propdata) <= cur_time:
            return

        # Retrieve data
        data_endpoint = f'{self._sc_name}_data_{endpoint}'
        data_propdata = self._win.getProperty(data_endpoint)
        if not data_propdata:
            return

        return data_loads(data_propdata)

    def _set_mem_cache(self, endpoint, expires, data):
        '''
            window property cache as alternative for memory cache
            usefull for (stateless) plugins
        '''
        if not self._memcache:
            return
        expr_endpoint = f'{self._sc_name}_expr_{endpoint}'
        data_endpoint = f'{self._sc_name}_data_{endpoint}'
        self._win.setProperty(expr_endpoint, str(expires))
        self._win.setProperty(data_endpoint, data)

    def _get_db_cache(self, endpoint, cur_time):
        '''get cache data from sqllite _database'''
        result = None
        query = "SELECT expires, data, checksum FROM simplecache WHERE id = ? LIMIT 1"
        cache_data = self._execute_sql(query, (endpoint,))
        if not cache_data:
            return
        cache_data = cache_data.fetchone()
        if not cache_data or int(cache_data[0]) <= cur_time:
            return
        try:
            data = str(zlib.decompress(cache_data[1]), 'utf-8')
        except TypeError:
            data = cache_data[1]
        self._set_mem_cache(endpoint, cache_data[0], data)
        result = data_loads(data)
        return result

    def _set_db_cache(self, endpoint, expires, data):
        ''' store cache data in _database '''
        query = "INSERT OR REPLACE INTO simplecache( id, expires, data, checksum) VALUES (?, ?, ?, ?)"
        data = zlib.compress(bytes(data, 'utf-8'))
        self._execute_sql(query, (endpoint, expires, data, 0))

    def _do_delete(self):
        '''perform cleanup task'''
        if self._exit or self._monitor.abortRequested():
            return

        self._win.setProperty(f'{self._sc_name}.cleanbusy', "busy")
        self.kodi_log(f'CACHE: Deleting {self._sc_name}...')

        with self.busy_tasks(__name__):
            query = 'DELETE FROM simplecache'
            self._execute_sql(query)
            self._execute_sql("VACUUM")

        # Washup
        cur_time = set_timestamp(0, True)
        self._win.setProperty(f'{self._sc_name}.clean.lastexecuted', str(cur_time))
        self._win.clearProperty(f'{self._sc_name}.cleanbusy')
        self.kodi_log(f'CACHE: Delete {self._sc_name} done')

    def _do_cleanup(self, force=False):
        '''perform cleanup task'''
        if self._exit or self._monitor.abortRequested():
            return

        if self._win.getProperty(f'{self._sc_name}.cleanbusy'):
            return

        self._win.setProperty(f'{self._sc_name}.cleanbusy', "busy")
        self.kodi_log(f"CACHE: Running cleanup...\n{self._sc_name}", 1)

        with self.busy_tasks(__name__):
            cur_time = set_timestamp(0, True)
            query = "SELECT id, expires FROM simplecache"
            for cache_data in self._execute_sql(query).fetchall():
                if self._exit or self._monitor.abortRequested():
                    return
                cache_id = cache_data[0]
                cache_expires = cache_data[1]
                # always cleanup all memory objects on each interval
                self._win.clearProperty(cache_id)
                # clean up db cache object only if expired
                if not force and int(cache_expires) >= cur_time:
                    continue
                query = 'DELETE FROM simplecache WHERE id = ?'
                self._execute_sql(query, (cache_id,))
                self.kodi_log(f'CACHE: delete from db {cache_id}')

            # compact db
            self._execute_sql("VACUUM")

        # Washup
        self._win.setProperty(f'{self._sc_name}.clean.lastexecuted', str(cur_time))
        self._win.clearProperty(f'{self._sc_name}.cleanbusy')
        self.kodi_log(f"CACHE: Cleanup complete...\n{self._sc_name}", 1)

    def _set_pragmas(self, connection):
        if not self._connection:
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA journal_mode=WAL")
        if self._re_use_con:
            self._connection = connection
        return connection

    def _get_database(self, attempts=2):
        '''get reference to our sqllite _database - performs basic integrity check'''
        try:
            connection = self._connection or sqlite3.connect(self._db_file, timeout=2.0, isolation_level=None, check_same_thread=not self._re_use_con)
            connection.execute('SELECT * FROM simplecache LIMIT 1')
            return self._set_pragmas(connection)
        except Exception:
            # our _database is corrupt or doesn't exist yet, we simply try to recreate it
            if xbmcvfs.exists(self._db_file):
                self.kodi_log(f'CACHE: Deleting Corrupt File: {self._db_file}...', 1)
                xbmcvfs.delete(self._db_file)
            try:
                self.kodi_log(f'CACHE: Initialising: {self._db_file}...', 1)
                connection = self._connection or sqlite3.connect(self._db_file, timeout=2.0, isolation_level=None, check_same_thread=not self._re_use_con)
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS simplecache(
                    id TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)""")
                connection.execute("CREATE INDEX idx ON simplecache(id)")
                return self._set_pragmas(connection)
            except Exception as error:
                self.kodi_log(f'CACHE: Exception while initializing _database: {error} ({attempts})\n{self._sc_name}', 1)
                if attempts < 1:
                    return
                attempts -= 1
                self._monitor.waitForAbort(1)
                return self._get_database(attempts)

    def _execute_sql(self, query, data=None):
        '''little wrapper around execute and executemany to just retry a db command if db is locked'''
        retries = self._retries

        def _database_execute(_database):
            if not data:
                return _database.execute(query)
            if isinstance(data, list):
                return _database.executemany(query, data)
            return _database.execute(query, data)

        # always use new db object because we need to be sure that data is available for other simplecache instances
        error = None
        with self._get_database() as _database:
            while retries > 0 and not self._monitor.abortRequested():
                if self._exit:
                    return None
                try:
                    return _database_execute(_database)
                except sqlite3.OperationalError as err:
                    error = f'{err}'
                except Exception as err:
                    error = f'{err}'
                if error is None:
                    continue
                if error != 'database is locked':
                    break
                retries = retries - 1
                if retries > 0:
                    log_level = 1 if retries < self._retries - 1 else 2  # Only debug log for first retry
                    transaction = 'commit' if data else 'lookup'
                    self.kodi_log(f'CACHE: _database LOCKED -- Retrying DB {transaction}...\n{self._sc_name}', log_level)
                    self._monitor.waitForAbort(self._retry_polling)
                    continue
                error = 'Retry failed. Database locked.'
        if error not in [None, 'not an error']:
            self.kodi_log(f'CACHE: _database ERROR! -- {error}\n{self._sc_name}', 1)
        return None

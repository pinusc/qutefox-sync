import argparse
import json
import yaml
import os
import logging
import hashlib
import math
import subprocess
import time
import sqlite3
from pathlib import Path
from datetime import datetime
from syncclient import client

logging.basicConfig(encoding='utf-8', level=logging.ERROR)
logger = logging.getLogger('qutefox')
logger.setLevel(logging.DEBUG)
# logging.getLogger().setLevel(logging.DEBUG)

FXA_CLIENT_NAME = 'Python Sync Client'
FXA_CLIENT_VERSION_MAJOR = '0.9'
CLIENT_NAME = 'qutesyncclient'


class UserScript():
    def __init__(self):
        self.mode = os.environ.get("QUTE_MODE")
        self.data_dir = os.environ.get("QUTE_DATA_DIR")
        self.config_dir = os.environ.get("QUTE_CONFIG_DIR")
        self.fifo = os.environ.get("QUTE_FIFO")

    def run_command(self, command, args):
        with open(self.fifo, 'w') as fifo:
            fifo.write(command + ' ' + args.join(' '))


if os.environ.get("QUTE_MODE"):
    userscript = UserScript()
    QUTEBROSER_DATA_DIR = userscript.data_dir
    QUTEBROSER_CONFIG_DIR = userscript.data_dir
else:
    userscript = None
    QUTEBROSER_DATA_DIR = Path(os.environ.get("XDG_DATA_HOME"))/'qutebrowser'
    QUTEBROSER_CONFIG_DIR = \
        Path(os.environ.get("XDG_CONFIG_HOME"))/'qutebrowser'
os.environ['FXA_SESSION_FILE'] = str(
    Path(os.environ.get("XDG_DATA_HOME"))/'qutefox-sync/fxa_session.json')


class QuteFoxClient():
    def __init__(self, login, client_id, token_ttl=3600,
                 send_qute_commands=False):
        # self.read_qute_history()
        # return
        self.init_sync_file()
        if self.last_sync:
            logger.info(f'Last sync: {self.last_sync.get("sync_time")}')
        else:
            logger.info('Syncing for the first time...')
        self.fxa_session = client.get_fxa_session(login)
        logger.debug('FXA session obtained')
        self.client_id = client_id
        # get an OAuth access token...
        self.access_token, _ = client.create_oauth_token(
            self.fxa_session, client_id, token_ttl=token_ttl,
            with_refresh=False)
        logger.debug('Access token obtained')

        # create an authorized sync client...
        self.sync_client = client.get_sync_client(
            self.fxa_session, self.client_id, self.access_token,
            token_ttl=token_ttl, auto_renew=True)
        logger.debug('Sync client initialized')

        self.device_id = self.ensure_client_registered()
        logger.info(f'Client registered, id: {self.device_id}')

        self.params = {
            'full': True,
            'decrypt': True,
        }
        self.send_qute_commands = send_qute_commands
        self.histdb = sqlite3.connect(QUTEBROSER_DATA_DIR/'history.sqlite')

    def init_sync_file(self):
        directory = Path(os.environ.get("XDG_DATA_HOME"))/'qutefox-sync'
        if not directory.is_dir():
            Path.mkdir(directory)
        sync_file = directory/'sync-info'
        last_sync = {}
        if Path.is_file(sync_file):
            try:
                with open(sync_file) as f:
                    last_sync = json.loads(f.read())
            except json.JSONDecodeError:
                logger.error('Sync file empty or corrupt, truncating.')
                sync_file.write_text('')
        else:
            Path.touch(sync_file)
        # TODO add checks that last_sync is well-formed
        self.sync_file = sync_file
        self.last_sync = last_sync

    def update_sync_file(self, key, val):
        if self.last_sync is None:
            self.last_sync = {}
        self.last_sync[key] = val
        logger.debug(f'Writing sync file at {self.sync_file}')
        with open(self.sync_file, 'w') as f:
            f.write(json.dumps(self.last_sync))

    def upload_qute_history(self):
        lastsynctime = self.last_sync.get('history_upsync_time', 0)
        entries = {}
        with self.histdb as db:
            for row in db.execute(
                    'SELECT * FROM History WHERE atime > %d;'
                    % int(lastsynctime)):
                entry = entries.get(row[0], {})
                url = row[0]
                entry['url'] = url
                entry['title'] = row[1]
                # we must multiply atime by 1000000 because firefox
                # uses 16-digit timestamps (microseconds) whereas
                # qutebrowser uses 10-digit timestamps (seconds)
                entry['visits'] = entry.get('visits', []) + \
                    [{'date': int(row[2]) * 10**6,
                      'type': 1 if row[3] == 0 else 6}]
                entries[url] = entry
        hist_res = self.sync_client.get_records(
            'history', parse_data=True, **self.params)
        hist_res = [json.loads(bso['payload']) for bso in hist_res]
        ff_hist_dict = {bso.get('histUri'): bso for bso in hist_res}
        bso_list = []
        for url in entries:
            qute_history_dict = entries[url]
            qute_visits = qute_history_dict['visits']
            bso = ff_hist_dict.get(url)
            if bso is not None and bso.get('visits') is not None:
                bso['visits'] += [v for v in qute_visits
                                  if v not in bso['visits']]
            else:
                # create BSO from scratch
                # TODO test if my own generated IDs correspond to FF's
                bso = {
                    'id': hashlib.sha1(url.encode('utf-8')).hexdigest()[:10],
                    'histUri': qute_history_dict['url'],
                    'title': qute_history_dict['title'],
                    'visits': qute_visits
                }
            bso['visits'].sort(key=lambda d: d.get('date'), reverse=True)
            bso_list.append(bso)
        with open('h.json', 'w') as f:
            f.write(json.dumps(bso_list))
        res = self.sync_client.post_records('history', bso_list, encrypt=True)
        __import__('pudb').set_trace()
        self.update_sync_file('history_upsync_time', time.time())

    def download_ff_history(self):
        hist_res = self.sync_client.get_records(
            'history', parse_data=True, **self.params)
        lastsynctime = self.last_sync.get('history_dsync_time', 0)
        # FIXME find out what type means
        tosync = []
        for record in hist_res:
            if record.get('modified', -1) > lastsynctime:
                tosync.append(json.loads(record.get('payload')))
        db_records = []
        for bso in tosync:
            # visit types:
            # 1: A link was followed.
            # 2: The URL was typed by the user.
            # 3: The user followed a bookmark.
            # 4: Some inner content was loaded.
            # 5: A permanent redirect was followed.
            # 6: A temporary redirect was followed.
            # 7: The URL was downloaded.
            # 8: User follows a link that was in a frame.
            if bso.get('histUri') is None:
                continue
            visits = [i for i in bso.get('visits', [])
                      if i.get('type') not in [4, 7, 8]
                      and int(i.get('date')) > lastsynctime]
            for visit in visits:
                # Note: we truncate the date because
                # FF sync uses microsecond (16-digit) timestamps
                # and qutebrowser uses second (10-digit) timestamps
                db_records.append(
                    (bso.get('histUri'),
                     bso.get('title', ''),
                     visit.get('date') // 1000000,
                     int(visit.get('type') in [5, 6])))
        with self.histdb as db:
            for record in db_records:
                db.execute('INSERT INTO History VALUES (?, ?, ?, ?);', record)
        logger.info(f"Added {len(db_records)} entries to qutebrowser history")
        self.update_sync_file('history_dsync_time', time.time())

    def ensure_client_registered(self):
        client_name = 'qutesyncclient'  # FIXME magic string

        devices = self.fxa_session.apiclient.get("/account/devices",
                                                 auth=self.fxa_session._auth)
        my_device = None

        for fxa_device in devices:
            if fxa_device['isCurrentDevice']:
                my_device = fxa_device

        device_id = "29a686c482ca7389604ac02aeb0fc7b3"
        device_id = my_device.get('id')
        # device_id = client.read_session_cache()['uid']

        bso = {
            'id': device_id,
            'fxaDeviceId': device_id,
            'name': client_name,
            'protocols': [
                '1.5'
            ],
            'type': 'desktop'
        }

        self.sync_client.post_record('clients', bso, encrypt=True, ttl=1814400)

        return device_id

    def _get_firefox_tabs(self):
        return self.sync_client.get_records('tabs', parse_data=True,
                                            **self.params)

    def create_qutebrowser_sessions(self):
        outer_json = self._get_firefox_tabs()
        logger.info('Obtained tab list from firefox')
        session_name_list = []
        for payload in outer_json:
            inner_json = json.loads(payload['payload'])
            if inner_json['id'] == self.device_id:
                continue
            client_name = inner_json['clientName']
            tablist = []
            for jsontab in inner_json['tabs']:
                tab = {}
                tab['children'] = []
                tab['collapsed'] = False
                tab['tab'] = {'history': [{
                    'active': True,
                    'pinned': False,
                    'scroll-pos': {
                        'x': 0,
                        'y': 0
                    },
                    'zoom': 1.0,
                    'title': jsontab['title'],
                    'url': jsontab['urlHistory'][0]
                }
                ]}
                tablist.append(tab)
            tabtree = {(i+1): v for (i, v) in enumerate(tablist)}
            tabtree[0] = {
                'children': list(tabtree.keys()),
                'collapsed': False,
                'tab': {}
            }
            session = {
                'windows': [{
                    'active': True,
                    'geometry': None,
                    'tree': tabtree
                }]
            }
            session_path = QUTEBROSER_DATA_DIR/f'sessions/{client_name}.yml'
            with open(session_path, 'w') as session_file:
                yaml.dump(session, session_file, default_flow_style=False)
            session_name_list.append(client_name)
        logger.info('Created qutebrowser sessions: ' +
                    ', '.join([f'"{s}"' for s in session_name_list]))

    def update_ff_session(self, session_name=None):
        if session_name:
            qsess = QUTEBROSER_DATA_DIR/f'sessions/{session_name}.yml'
        elif userscript is not None or self.send_qute_commands:
            if userscript is not None:
                # simple hack to get current session: force qutebrowser to
                # save it then upload the most recently written session file
                userscript.run_command('session-save')
            elif self.send_qute_commands:
                self.qutebrowser_command(':session-save')
            session_list = [(f.stat().st_mtime, f)
                            for f in (qsess.data_dir/'sessions').iterdir()]
            qsess = session_list.sort(lambda x: x[0])[-1]
        else:
            qsess = QUTEBROSER_DATA_DIR/'sessions/default.yml'
        logger.info('Uploading qutebrowser session ' + qsess.name)
        with open(qsess) as qsess:
            qsess_yaml = qsess.read()
            qute_session = yaml.safe_load(qsess_yaml)

        tabs = []
        for window in qute_session['windows']:
            for tab_n in window['tree'].keys():
                tab = window['tree'][tab_n]['tab']
                if tab == {}:
                    continue
                tab_hist = sorted(
                    tab['history'], key=lambda x: x['last_visited'])
                if len(tab_hist) == 0:
                    continue
                last_page = tab_hist[-1]
                last_used = datetime.fromisoformat(last_page['last_visited'])
                tabs.append({
                    'icon': None,
                    'lastUsed': int(last_used.timestamp()),
                    'title': last_page['title'],
                    'urlHistory': [last_page['url']]
                })
        logger.debug('Session converted in BSO record')
        # rec_id = sha1(bytes(str(tabs), 'utf-8')).hexdigest()
        tab_object = {
            'id': self.device_id,
            'clientName': CLIENT_NAME,
            'tabs': tabs
        }
        self.sync_client.post_record(
            'tabs', tab_object, encrypt=True)
        logger.debug('Session record posted to SyncServer')

    def reload_qutebrowser_bookmarks(self):
        reload_filename = Path(__file__).parent/'util/bookmark_reload.py'
        self.qutebrowser_command(f':debug-pyeval --file {reload_filename}')

    def qutebrowser_command(self, command):
        subprocess.run(['qutebrowser', f'{command}'])

    def download_ff_bookmarks(self, folder_id):
        ff_bookmark_raw = self.sync_client.get_records(
            'bookmarks', parse_data=True, **self.params)
        ff_bookmark_response = [json.loads(bso.get('payload'))
                                for bso in ff_bookmark_raw]
        ff_folders = [bso for bso in ff_bookmark_response
                      if bso.get('id') == folder_id]
        if len(ff_folders) != 1:
            if not ff_folders:
                raise KeyError('Bookmark folder not found')
            else:
                logger.error(
                    'Multiple matching folders found, critical sync error')
        folder = ff_folders[0]
        ff_bookmarks = []
        for child_id in folder.get('children', []):
            child = next(bso for bso in ff_bookmark_response
                         if bso.get('id') == child_id)
            if child.get('type') == 'bookmark':
                if not child.get('bmkUri'):
                    logger.warning(
                        f'bmkUri not found for bookmark record {child_id}')
                    continue
                ff_bookmarks.append(
                    (child.get('bmkUri'), child.get('title', '')))
        logger.info(f'Gotten {len(ff_bookmarks)} Firefox bookmarks')
        bookfile = QUTEBROSER_CONFIG_DIR/'bookmarks/urls'
        qute_bookmarks = []
        with open(bookfile) as f:
            for line in f:
                url, *title = line.split(' ')
                title = ' '.join(title)
                qute_bookmarks.append((url, title))
        new_bookmark_lines = [' '.join(b) for b in ff_bookmarks
                              if b not in qute_bookmarks]
        logger.info(f'Updating {len(new_bookmark_lines)} bookmarks')
        with open(bookfile, 'a') as f:
            f.write('\n'.join(new_bookmark_lines))
        logger.info('Reloading qutebrowser bookmarks (hacky, might not work)')
        if self.send_qute_commands:
            self.reload_qutebrowser_bookmarks()

    def upload_qute_bookmarks(self,
                              parent={'id': 'menu', 'name': 'menu'},
                              force=False,
                              folder_name='qutebrowser'):
        """
        Args:
            folder_name: the name of the folder to save qutebrowser's bookmarks
                into. This will be a child of parent
            parent: a dict with keys 'name' and 'id', directing where the
                folder containing synced bookmarks will be created/updated.

        """
        bookfile = QUTEBROSER_CONFIG_DIR/'bookmarks/urls'
        if not bookfile.is_file():
            logger.error(f'Bookmark file {bookfile} not found, returning.')
            return
        if bookfile.stat().st_mtime < \
                self.last_sync.get('bookmark_upsync_time', 0) and not force:
            logger.info('Bookmark file older than last sync, not uploading.')
        if not (parent.get('id') or parent.get('name')):
            raise ValueError("'parent' must contain both 'id' and 'name'")
        timenow = math.floor(datetime.now().timestamp() * 1000)
        folder_id = hashlib.sha1(folder_name.encode('utf-8')).hexdigest()[:10]

        # obtain existing bookmark records and check if a directory
        # already exists from previous sync. Store it in ff_folder_bso
        ff_bookmark_response = self.sync_client.get_records(
            'bookmarks', parse_data=True, **self.params)
        ff_bookmarks = [json.loads(bso['payload'])
                        for bso in ff_bookmark_response]
        ff_bookmark_ids = [bso.get('id') for bso in ff_bookmarks]
        folder_match = [bso for bso in ff_bookmarks
                        if bso.get('type') == 'folder'
                        and bso.get('title') == folder_name
                        and bso.get('parentid') == parent['id']]
        if len(folder_match) > 1:
            logger.warning(
                f'Multiple directories with same name: {folder_name}')
            # FIXME actually select matching id
            logger.warning(
                'Searching for matching id or creating a new one...')
            folder_match = [bso for bso in folder_match
                            if bso.get('id') == folder_id]
        if folder_match:
            folder_bso = folder_match[0]
            logger.info(
                f'Found an existing folder record with id {folder_bso["id"]}')
        else:
            folder_bso = {
                'id': folder_id,
                'parentName': parent['name'],
                'parentid': parent['id'],
                'title': folder_name,
                'type': 'folder',
                'dateAdded': timenow
            }
            logger.info(
                f'Creating a new folder record with id {folder_bso["id"]}')
        bookmarks = []
        with open(bookfile) as f:
            for line in f:
                url, *title = line.split(' ')
                title = ' '.join(title)
                # qutebrowser enforces no url duplicates
                # so we can obtain a unique ID from the (unique) url
                bso = {
                    'type': 'bookmark',
                    'parentid': folder_bso['id'],
                    'parentName': folder_bso['title'],
                    'title': title,
                    'bmkUri': url,
                    'id': hashlib.sha1(url.encode('utf-8')).hexdigest()[:10],
                    'loadInSidebar': False,
                    'dateAdded': timenow,
                    'tags': []
                }
                if bso['id'] not in folder_bso.get('children', []) \
                        or bso['id'] not in ff_bookmark_ids:
                    bookmarks.append(bso)
        if not bookmarks:
            logger.info('All bookmarks up to date!')
            return
        folder_bso['children'] = folder_bso.get('children', []) \
            + [b['id'] for b in bookmarks
               if b not in folder_bso.get('children', [])]

        logger.info('Uploading folder')
        response_str = self.sync_client.post_record(
            'bookmarks', folder_bso, encrypt=True, params={'batch': 'true'})
        response = json.loads(response_str)
        if response['failed']:
            print(response)
            return
        batch_id = response.get('batch')

        logger.info('Uploading new individual bookmarks')
        for i, bookmark in enumerate(bookmarks):
            logger.debug(f"POSTing record with id {bookmark['id']}")
            params = {'batch': batch_id}
            if i == len(bookmarks) - 1:
                params['commit'] = 'true'
            res = self.sync_client.post_record(
                'bookmarks', bookmark, encrypt=True, params=params)

        logger.info('Upload completed with status: ' + res)
        self.update_sync_file('bookmark_upsync_time', int(time.time()))


def main():
    parser = argparse.ArgumentParser(
        description="""CLI to interact with Firefox Sync""",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    # API interaction options...
    parser.add_argument('-c', '--client-id', dest='client_id', required=True,
                        help='The client_id to use for OAuth (mandatory).')
    parser.add_argument('-u', '--user', dest='login',
                        help='Firefox Accounts login (email address).')
    parser.add_argument('command', choices=['sync', 'sync-bookmarks',
                                            'sync-history', 'noop'])
    parser.add_argument('--token-ttl', dest='token_ttl', type=int,
                        default=3600,
                        help='The validity of the OAuth token in seconds')
    parser.add_argument('--one-way-to', dest='one_way_dest',
                        choices=['qutebrowser', 'firefox'],
                        help='Only sync one way. ')
    parser.add_argument('--bookmark-folder-name', dest='bookmark_folder_name')
    parser.add_argument('--bookmark-folder-id', dest='bookmark_folder_id')
    parser.add_argument('--bookmark-folder-parent',
                        dest='bookmark_folder_parent',
                        nargs=2, metavar=('ID', 'NAME'))
    parser.add_argument('--send-qute-commands', type=bool, default=False,
                        help='Before/after syncing files, send commands to' +
                        'qutebrowser to update them')

    args, extra = parser.parse_known_args()

    qutefox = QuteFoxClient(args.login, args.client_id,
                            token_ttl=args.token_ttl,
                            send_qute_commands=args.send_qute_commands)

    if args.command == 'sync':
        if args.one_way_dest is None or args.one_way_dest == 'qutebrowser':
            qutefox.create_qutebrowser_sessions()
        if args.one_way_dest is None or args.one_way_dest == 'firefox':
            qutefox.update_ff_session()
    if args.command == 'sync-bookmarks':
        upload_bookmark_args = {}
        if args.bookmark_folder_name:
            upload_bookmark_args['folder_name'] = args.bookmark_folder_name
        if args.bookmark_folder_parent:
            upload_bookmark_args['parent'] = {
                'id': args.bookmark_folder_parent[0],
                'name': args.bookmark_folder_parent[1]
            }
        download_bookmark_args = {}
        if args.bookmark_folder_id:
            download_bookmark_args['folder_id'] = args.bookmark_folder_id
        # qutefox.upload_qute_bookmarks(**upload_bookmark_args)
        qutefox.download_ff_bookmarks(**download_bookmark_args)
    if args.command == 'sync-history':
        # qutefox.download_ff_history()
        qutefox.upload_qute_history()


if __name__ == "__main__":
    main()

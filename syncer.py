import argparse
import json
import yaml
import os
import logging
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
        self.fifo = os.environ.get("QUTE_FIFO")

    def run_command(self, command, args):
        with open(self.fifo, 'w') as fifo:
            fifo.write(command + ' ' + args.join(' '))


if os.environ.get("QUTE_MODE"):
    userscript = UserScript()
    QUTEBROSER_DATA_DIR = userscript.data_dir
else:
    userscript = None
    QUTEBROSER_DATA_DIR = Path(os.environ.get("XDG_DATA_HOME"))/'qutebrowser'


class QuteFoxClient():
    def __init__(self, login, client_id, token_ttl=3600):
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
        print(device_id)

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
        elif userscript is not None:
            # simple hack to get current session: force qutebrowser to write it
            # then upload the most recently written session file
            userscript.run_command('session-save')
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
                tab_hist = sorted(tab['history'], key=lambda x: x['last_visited'])
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
    parser.add_argument('command', choices=['sync'])
    parser.add_argument('--token-ttl', dest='token_ttl', type=int,
                        default=3600,
                        help='The validity of the OAuth token in seconds')
    parser.add_argument('--one-way-to', dest='one_way_dest',
                        choices=['qutebrowser', 'firefox'],
                        help='Only sync one way. ')

    args, extra = parser.parse_known_args()

    qutefox = QuteFoxClient(args.login, args.client_id,
                            token_ttl=args.token_ttl)

    if args.command == 'sync':
        if args.one_way_dest is None or args.one_way_dest == 'qutebrowser':
            qutefox.create_qutebrowser_sessions()
        if args.one_way_dest is None or args.one_way_dest == 'firefox':
            qutefox.update_ff_session()


if __name__ == "__main__":
    main()

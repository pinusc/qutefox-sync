import argparse
import json
import yaml
from syncclient import client
from pprint import pprint


def create_qutebrowser_sessions(tabs_json):
    outer_json = json.loads(tabs_json)
    for payload in outer_json:
        inner_json = json.loads(payload['payload'])
        # __import__('pdb').set_trace()
        client_name = inner_json['clientName']
        print(client_name)
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
        with open(f'/home/pinusc/.local/share/qutebrowser/sessions/{client_name}.yml', 'w') as outfile:
            yaml.dump(session, outfile, default_flow_style=False)





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
    parser.add_argument('--token-ttl', dest='token_ttl', type=int, default=300,
                        help='The validity of the OAuth token in seconds')

    args, extra = parser.parse_known_args()

    params = {}

    params["full"] = True
    params["decrypt"] = True
    fxa_session = client.get_fxa_session(args.login)

    # get an OAuth access token...
    (access_token, _) = client.create_oauth_token(fxa_session, args.client_id,
                                                  token_ttl=args.token_ttl,
                                                  with_refresh=False)

    # create an authorized sync client...
    sync_client = client.get_sync_client(fxa_session, args.client_id,
                                         access_token,
                                         token_ttl=args.token_ttl,
                                         auto_renew=True)

    tabs = sync_client.get_records('tabs', **params)
    create_qutebrowser_session(tabs_json)
    print(tabs)


if __name__ == "__main__":
    # main()
    with open('tabres.json') as tabres:
        tabs_json = tabres.read()
        create_qutebrowser_sessions(tabs_json)

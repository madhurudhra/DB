import asyncio, json, os, re
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
try:
    from mcp.client.streamable_http import streamablehttp_client as http_client
except ImportError:
    from mcp.client.streamable_http import streamable_http_client as http_client
URL = 'https://agent.robinhood.com/mcp/trading'
TOKENS = Path(os.getenv('RH_MCP_TOKEN_FILE', '/var/lib/super-intelligence/rh_mcp_tokens.json'))
REDIRECT = 'http://localhost:3030/callback'

class FileStorage(TokenStorage):

    def _load(self):
        try:
            return json.loads(TOKENS.read_text())
        except Exception:
            return {}

    def _save(self, d):
        TOKENS.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(TOKENS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 384)
        with os.fdopen(fd, 'w') as f:
            json.dump(d, f)
        os.chmod(TOKENS, 384)

    async def get_tokens(self):
        d = self._load().get('tokens')
        return OAuthToken(**d) if d else None

    async def set_tokens(self, tokens):
        d = self._load()
        d['tokens'] = tokens.model_dump(mode='json')
        self._save(d)

    async def get_client_info(self):
        d = self._load().get('client')
        return OAuthClientInformationFull(**d) if d else None

    async def set_client_info(self, info):
        d = self._load()
        d['client'] = info.model_dump(mode='json')
        self._save(d)

async def redirect_handler(url):
    print('\n1) Open this URL in your desktop browser and approve access:\n\n' + str(url) + '\n')

async def callback_handler():
    print('2) After approving, the browser will fail to load http://localhost:3030/callback?...')
    print('   That is expected. Copy the FULL address from the address bar and paste it here.')
    raw = (await asyncio.to_thread(input, 'Paste URL: ')).strip()
    q = parse_qs(urlparse(raw).query)
    return (q['code'][0], (q.get('state') or [None])[0])

def mask(s):
    return re.sub('\\d{6,}', lambda m: '*' * (len(m.group()) - 4) + m.group()[-4:], s)

async def main():
    meta = OAuthClientMetadata(client_name='super-intelligence-vps', redirect_uris=[REDIRECT], grant_types=['authorization_code', 'refresh_token'], response_types=['code'], token_endpoint_auth_method='none')
    auth = OAuthClientProvider(server_url=URL, client_metadata=meta, storage=FileStorage(), redirect_handler=redirect_handler, callback_handler=callback_handler)
    async with http_client(URL, auth=auth) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print('TOOLS:', [t.name for t in tools])
            for t in tools:
                if t.name in ('place_equity_order', 'review_equity_order'):
                    print('\nSCHEMA', t.name, json.dumps(t.inputSchema))
            r = await s.call_tool('get_accounts', {})
            print('\nACCOUNTS (masked):', mask(str(r.content))[:1500])
if __name__ == '__main__':
    asyncio.run(main())

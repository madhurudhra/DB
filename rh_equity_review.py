import asyncio, json, os, sys
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata
from rh_mcp_auth import FileStorage, URL, REDIRECT, http_client, mask
LAST4 = os.getenv('EQUITY_ACCOUNT_LAST4', '5004')

async def _no_login(*a, **k):
    raise RuntimeError('LOGIN_REQUIRED: stored token missing or refresh failed')

async def call(tool, args):
    meta = OAuthClientMetadata(client_name='super-intelligence-vps', redirect_uris=[REDIRECT], grant_types=['authorization_code', 'refresh_token'], response_types=['code'], token_endpoint_auth_method='none')
    auth = OAuthClientProvider(server_url=URL, client_metadata=meta, storage=FileStorage(), redirect_handler=_no_login, callback_handler=_no_login)
    import httpx2
    async with httpx2.AsyncClient(auth=auth, timeout=httpx2.Timeout(30.0, read=60.0)) as authenticated_http:
        async with http_client(URL, http_client=authenticated_http) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                r = await s.call_tool(tool, args)
                if getattr(r, 'isError', False):
                    raise RuntimeError(f'MCP_TOOL_ERROR: {tool}; submission outcome must be reconciled before any retry')
                return ''.join((getattr(c, 'text', str(c)) for c in r.content))

async def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else 'IONQ'
    accts = json.loads(await call('get_accounts', {}))['data']['accounts']
    ok = [a for a in accts if a['account_number'].endswith(LAST4) and a.get('agentic_allowed')]
    if len(ok) != 1:
        sys.exit('Expected exactly one agentic account ending ' + LAST4)
    out = await call('review_equity_order', {'account_number': ok[0]['account_number'], 'symbol': symbol, 'side': 'buy', 'type': 'market', 'dollar_amount': '1.00'})
    print('REVIEW (nothing placed):', mask(out)[:3000])
if __name__ == '__main__':
    asyncio.run(main())

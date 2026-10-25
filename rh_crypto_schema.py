import asyncio, json
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata
from rh_mcp_auth import FileStorage, URL, REDIRECT, http_client
from rh_equity_review import _no_login

async def main():
    meta = OAuthClientMetadata(client_name='super-intelligence-vps', redirect_uris=[REDIRECT], grant_types=['authorization_code', 'refresh_token'], response_types=['code'], token_endpoint_auth_method='none')
    auth = OAuthClientProvider(server_url=URL, client_metadata=meta, storage=FileStorage(), redirect_handler=_no_login, callback_handler=_no_login)
    async with http_client(URL, auth=auth) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            for t in (await s.list_tools()).tools:
                if t.name in ('get_crypto_orders', 'get_crypto_positions'):
                    print(t.name, json.dumps(t.inputSchema))
if __name__ == '__main__':
    asyncio.run(main())

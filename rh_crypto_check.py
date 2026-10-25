import asyncio, json
from rh_equity_review import call
from rh_mcp_auth import mask

async def main():
    accts = json.loads(await call('get_accounts', {}))['data']['accounts']
    mcp = [a for a in accts if a.get('agentic_allowed')][0]
    for args in ({'account_number': mcp['account_number']}, {'account_number': mcp.get('rhc_account_number', '')}, {}):
        try:
            out = await call('get_crypto_orders', args)
            print('OK with', list(args))
            print(mask(out)[:3000])
            return
        except Exception as e:
            print('failed with', list(args), '->', str(e)[:300])
if __name__ == '__main__':
    asyncio.run(main())

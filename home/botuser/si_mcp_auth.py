import asyncio
import getpass
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

SERVER = "https://agent.robinhood.com/mcp/trading"
REDIRECT = "http://localhost:3030/callback"
STORE = Path.home() / ".config/super-intelligence/rh_mcp_tokens.json"


class FileStorage:
    def _load(self):
        if not STORE.exists():
            return {}
        return json.loads(STORE.read_text())

    def _save(self, data):
        STORE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = STORE.with_suffix(".tmp")
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w") as output:
            json.dump(data, output)
        os.chmod(temporary, 0o600)
        os.replace(temporary, STORE)

    async def get_tokens(self):
        value = self._load().get("tokens")
        return OAuthToken(**value) if value else None

    async def set_tokens(self, tokens):
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json")
        self._save(data)

    async def get_client_info(self):
        value = self._load().get("client")
        return OAuthClientInformationFull(**value) if value else None

    async def set_client_info(self, client_info):
        data = self._load()
        data["client"] = client_info.model_dump(mode="json")
        self._save(data)


async def show_authorization_url(url):
    print("\nOpen this authorization URL in your own browser:")
    print(url)
    print(
        "\nVerify the destination before signing in. "
        "Approve only the access you intend to grant."
    )


async def receive_callback():
    print(
        "\nAfter approval, the browser may fail to open localhost:3030.\n"
        "Copy the full callback URL from its address bar.\n"
        "Paste it into this VPS terminal only—not into chat."
    )
    raw = await asyncio.to_thread(
        getpass.getpass, "Callback URL (hidden): "
    )
    parsed = urlsplit(raw.strip())
    expected = urlsplit(REDIRECT)
    if (
        parsed.scheme,
        parsed.netloc,
        parsed.path,
    ) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        raise ValueError("Unexpected callback address")

    params = parse_qs(parsed.query)
    if "error" in params:
        raise ValueError("Authorization was declined or failed")

    for field in ("code", "state"):
        if len(params.get(field, [])) != 1:
            raise ValueError("Missing or ambiguous callback parameter")

    return AuthorizationCodeResult(
        code=params["code"][0],
        state=params["state"][0],
        iss=params.get("iss", [None])[0],
    )


async def main():
    oauth = OAuthClientProvider(
        server_url=SERVER,
        client_metadata=OAuthClientMetadata(
            client_name="super-intelligence-vps",
            redirect_uris=[REDIRECT],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=FileStorage(),
        redirect_handler=show_authorization_url,
        callback_handler=receive_callback,
    )

    async with httpx2.AsyncClient(
        auth=oauth,
        timeout=httpx2.Timeout(30.0, read=120.0),
    ) as client:
        async with streamable_http_client(
            SERVER, http_client=client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                print("\nAuthenticated MCP initialization: OK")
                print("No trading tools were called. No orders were submitted.")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as error:
        print(f"\nAuthentication did not complete: {type(error).__name__}")
        print(
            "Do not share callback URLs or token files. "
            "Report the error type shown above."
        )
        raise SystemExit(1)

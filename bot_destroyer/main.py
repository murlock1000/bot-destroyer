import asyncio
import logging
import os
import sys

from aiohttp import ClientConnectionError, ServerDisconnectedError
from nio import (
    AsyncClient,
    AsyncClientConfig,
    LocalProtocolError,
    LoginError,
    RoomMemberEvent,
    InviteMemberEvent,
    RoomMessageText
)
from bot_destroyer import destroy_loop

from bot_destroyer.callbacks import Callbacks
from bot_destroyer.config import Config
from bot_destroyer.storage import Storage

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
IMAGE_DIR = os.path.dirname(PROJECT_DIR)

logger = logging.getLogger(__name__)


async def main(args):
    """The first function that is run when starting the bot"""

    # Read user-configured options from a config file.
    # A different config file path can be specified as the first command line argument
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    else:
        config_path = "config.yaml"
    # Read the parsed config file and create a Config object
    config = Config(config_path)

    # Configure the database
    static_database = config.database
  #  static_database["connection_string"] = os.path.join(
   #     PROJECT_DIR, static_database["connection_string"]
   # )
    store = Storage(static_database)

    # Configuration options for the AsyncClient
    client_config = AsyncClientConfig(
        max_limit_exceeded=0,
        max_timeouts=0,
        store_sync_tokens=True,
        encryption_enabled=True,
    )

    # Initialize the matrix client
    client = AsyncClient(
        config.homeserver_url,
        config.user_id,
        device_id=config.device_id,
        store_path=config.store_path,
        config=client_config,
    )

    if config.user_token:
        client.access_token = config.user_token
        client.user_id = config.user_id

    client.user_name = config.user_name

    # Set up event callbacks for receiving room member events
    callbacks = Callbacks(client, store, config)
    client.add_event_callback(callbacks.invite, (InviteMemberEvent,))
    client.add_event_callback(callbacks.message, (RoomMessageText,))

    # Keep trying to reconnect on failure (with some time in-between)
    try:
        if config.user_token:
            # Use token to log in
            client.load_store()
            # Sync encryption keys with the server
            if client.should_upload_keys:
                await client.keys_upload()
        else:
            # Try to login with the configured username/password
            try:
                login_response = await client.login(
                    password=config.user_password,
                    device_name=config.device_name,
                )
                # Check if login failed
                if type(login_response) == LoginError:
                    logger.error("Failed to login: %s", login_response.message)
                    return -1
            except LocalProtocolError as e:
                # There's an edge case here where the user hasn't installed the correct C
                # dependencies. In that case, a LocalProtocolError is raised on login.
                logger.fatal(
                    "Failed to login. Have you installed the correct dependencies? "
                    "https://github.com/poljar/matrix-nio#installation "
                    "Error: %s",
                    e,
                )
                return -1

        # Login succeeded!
        logger.info(f"Logged in as {config.user_id}")

        # Create tasks for bot to perform asynchronously
        async def after_first_sync(client: AsyncClient):
            await client.synced.wait()
            client.destroyer = destroy_loop.Destroyer(client, store)

        sync_forever_task = asyncio.create_task(
            client.sync_forever(60000, full_state=True, loop_sleep_time=30000)
        )
        callbacks.main_loop = sync_forever_task

        after_first_sync_task = asyncio.create_task(
            after_first_sync(client)
        )

        await asyncio.gather(
            after_first_sync_task,
            sync_forever_task,
        )

    except (ClientConnectionError, ServerDisconnectedError):
        logger.warning("Unable to connect to homeserver")
        # Sleep so we don't bombard the server with login requests
        await client.close()
        # Exit with error code 255
        sys.exit(-1)
    finally:
        # Make sure to close the client connection on disconnect
        logger.info("Exiting")
        await client.close()

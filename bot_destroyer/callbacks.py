from __future__ import annotations
from typing import TYPE_CHECKING

import asyncio
import logging
from datetime import datetime

# noinspection PyPackageRequirements
from nio import (MatrixRoom, 
                RoomCreateResponse,
                RoomMemberEvent,
                InviteMemberEvent,
                JoinError,
                MegolmEvent,
                RoomMessageText,
                AsyncClient,
                )
from bot_destroyer.bot_commands import Command

from bot_destroyer.chat_functions import (
    create_private_room,
    find_private_msg,
    send_file_to_room,
    send_text_to_room,
)
from bot_destroyer.config import Config
from bot_destroyer.storage import Storage
from bot_destroyer.utils import with_ratelimit

logger = logging.getLogger(__name__)

DUPLICATES_CACHE_SIZE = 1000


class Callbacks(object):
    def __init__(self, client: AsyncClient, store: Storage, config: Config):
        """
        Args:
            client (nio.AsyncClient): nio client used to interact with matrix
            store (Storage): Bot storage
            config (Config): Bot configuration parameters
        """
        self.client = client
        self.store = store
        self.config = config
        self.command_prefix = config.command_prefix
        self.received_events = []

        self.rooms_pending = {}
        self.user_rooms_pending = {}
        self.lock = asyncio.Lock()
        self.items_to_send = 0
        self.main_loop = None

    def trim_duplicates_caches(self):
        if len(self.received_events) > DUPLICATES_CACHE_SIZE:
            self.received_events = self.received_events[:DUPLICATES_CACHE_SIZE]

    def should_process(self, event_id: str) -> bool:
        logger.debug("Callback received event: %s", event_id)
        if event_id in self.received_events:
            logger.debug("Skipping %s as it's already processed", event_id)
            return False
        self.received_events.insert(0, event_id)
        return True

    async def message(self, room: MatrixRoom, event: RoomMessageText):
        # If ignoring old messages, ignore messages older than 5 minutes
        if (datetime.now() - datetime.fromtimestamp(event.server_timestamp / 1000.0)).total_seconds() > 300:
            return
        
        self.trim_duplicates_caches()
        if self.should_process(event.event_id) is False:
            return
        
        await self._message(room, event)
        
    async def _message(self, room, event):
        # Extract the message text
        msg = event.body

        # Ignore messages from ourselves
        if event.sender == self.client.user:
            return

        # If this looks like an edit, strip the edit prefix
        if msg.startswith(" * "):
            msg = msg[3:]

        # Process as message if in a public room without command prefix
        # TODO Implement check of named commands using an array
        has_command_prefix = msg.startswith(self.command_prefix)

        if has_command_prefix:
            msg = msg[len(self.command_prefix):]
            command = Command(self.client, self.store, self.config, msg, room, event)
            await command.process()

    async def invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """Callback for when an invite is received. Join the room specified in the invite.
        Args:
            room: The room that we are invited to.
            event: The invite event.
        """
        
        if event.state_key != self.client.user_id:
            # Ignore not our own membership (invite) events
            return
        
        logger.debug(f"Got invite to {room.room_id} from {event.sender}.")

        # Attempt to join 3 times before giving up
        for attempt in range(3):
            result = await self.client.join(room.room_id)
            if type(result) == JoinError:
                logger.error(
                    f"Error joining room {room.room_id} (attempt %d): %s",
                    attempt,
                    result.message,
                )
            else:
                break
        else:
            logger.error("Unable to join room: %s", room.room_id)

        # Successfully joined room
        logger.info(f"Joined {room.room_id}")
        # Send out key request for encrypted events in room so we can accept the m.forwarded_room_key event
        response = await self.client.sync()
        for joined_room_id, room_info in response.rooms.join.items():
            if joined_room_id == room.room_id:
                for ev in room_info.timeline.events:
                    if type(ev) is MegolmEvent:
                        try:
                            # Request keys for the event and update the client store
                            if ev in self.client.outgoing_key_requests:
                                logger.debug("popping the session request")
                                self.client.outgoing_key_requests.pop(ev.session_id)
                            room_key_response = await self.client.request_room_key(ev)
                            await self.client.receive_response(room_key_response)
                        except Exception as e:
                            logger.info(f"Error requesting key for event: {e}")

    async def member(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        """Callback for when a room member event is received.
        Args:
            room (nio.rooms.MatrixRoom): The room the event came from
            event (nio.events.room_events.RoomMemberEvent): The event
        """
        logger.debug(
            f"Received a room member event for {room.display_name} | "
            f"{event.sender}: {event.membership}"
        )
        async with self.lock:
            self.trim_duplicates_caches()
            if self.should_process(event.event_id) is False:
                return

            # Ignore messages older than 15 seconds
            if (
                datetime.now() - datetime.fromtimestamp(event.server_timestamp / 1000.0)
            ).total_seconds() > 15:
                logger.debug("Ignoring old member event")
                return

            # Ignore if it was not us sending the invite
            if event.sender != self.client.user:
                logger.debug("Ignoring member event since it was not sent by us")
                return

            # Ignore if any other membership event
            if (
                event.membership != "invite"
            ):  # or event.prev_content is None or event.prev_content.get("membership") == "join":
                logger.debug("Ignoring due to not being an invite")
                return

            # Ignore event, if no messages waiting to be processed
            if room.room_id not in self.rooms_pending.keys():
                logger.debug("Ignoring due to no messages waiting to be processed")
                return

            # Check if this is the invited user, other than us
            receiving_user = event.state_key
            if receiving_user not in self.user_rooms_pending.keys():
                logger.debug("Ignoring, since invited user does not have pending rooms")
                return
            elif room.room_id not in self.user_rooms_pending[receiving_user]:
                logger.debug(
                    "Ignoring, since room id does not match any pending room in user room queue"
                )
                return

            logger.debug(
                f"Received invite to: {room.room_id}"
                f"Pending messages for room / Total messages: {len(self.rooms_pending[room.room_id])} / {self.items_to_send}"
            )

            # Send all pending messages for the room
            for message_task in self.rooms_pending[room.room_id]:
                await message_task  # TODO: Missing error handling here
                self.items_to_send -= 1

            # Clear processed room messages from queue
            self.rooms_pending.pop(room.room_id)
            self.user_rooms_pending[receiving_user].remove(room.room_id)

            # If user has no more pending rooms - remove from queue
            if len(self.user_rooms_pending[receiving_user]) == 0:
                self.user_rooms_pending.pop(receiving_user)

            # Check if that was the last messages to be sent - exit the program.
            if self.items_to_send == 0:
                self.main_loop.cancel()

    # Code adapted from - https://github.com/vranki/hemppa/blob/dcd69da85f10a60a8eb51670009e7d6829639a2a/bot.py
    async def send_msg(
        self,
        mxid: str,
        content: str,
        message_type: str,
        room_id: str = None,
        roomname: str = "",
    ):
        """
        :param mxid: A Matrix user id to send the message to
        :param roomname: A Matrix room id to send the message to
        :param message: Text to be sent as message
        :return bool: Returns room id upon sending the message
        """

        room_initialized = True

        # Acquire lock to process room for user - so duplicate room requests are not sent.
        async with self.lock:
            # Sends private message to user. Returns true on success.
            if room_id is None:
                logger.debug(f"Searching for an existing room for {mxid}")
                msg_room = find_private_msg(self.client, mxid)
                if msg_room is not None:
                    room_id = msg_room.room_id
                    logger.debug(f"Found existing room for {mxid}: {room_id}")
                elif mxid in self.user_rooms_pending.keys():
                    room_id = self.user_rooms_pending[mxid]
                    logger.debug(f"Room is being created for {mxid}: {room_id}")
                    room_initialized = False

            # If an existing room was not found - create a new one.
            if room_id is None:
                logger.debug(f"Creating a new room for {mxid}")
                resp = await create_private_room(self.client, mxid, roomname)

                if isinstance(resp, RoomCreateResponse):
                    room_id = resp.room_id
                    room_initialized = False
                    if room_id not in self.rooms_pending.keys():
                        self.rooms_pending[room_id] = []
                else:
                    logger.error(f"Failed to create room for {mxid}")
                    return

            task = None

            # Determine task type
            if message_type == "text":
                task = with_ratelimit(send_text_to_room)(self.client, room_id, content)
            elif message_type == "image":
                task = with_ratelimit(send_file_to_room)(self.client, room_id, content, "m.image")
            elif message_type == "file":
                task = with_ratelimit(send_file_to_room)(self.client, room_id, content, "m.file")
            else:
                logger.error(f"Unknown message type: {message_type}")
                return

            # Based on if the room is initialized - execute the task now, or defer execution until user has been invited to the room
            if room_initialized:
                await task
                logger.debug(f"Message sent to {mxid} in room {room_id}")

                # Decrement task counter
                self.items_to_send -= 1

                # Check if that was the last messages to be sent - exit the program.
                if self.items_to_send == 0:
                    self.main_loop.cancel()
            else:
                self.rooms_pending[room_id].append(task)
                if mxid not in self.user_rooms_pending.keys():
                    self.user_rooms_pending[mxid] = [room_id]

                logger.debug(
                    f"Message appended to queue to be sent to {mxid} in room {room_id}"
                )

            logger.debug(
                f"Messages left to send: {self.items_to_send}"
                f"Room message queue: {self.rooms_pending}"
                f"Pending User room queue: {self.user_rooms_pending}"
            )

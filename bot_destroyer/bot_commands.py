from nio import AsyncClient, MatrixRoom, RoomMessageText
from bot_destroyer import commands_help

from bot_destroyer.chat_functions import react_to_event, send_text_to_room
from bot_destroyer.config import Config
from bot_destroyer.destroy_loop import Destroyer, Room
from bot_destroyer.storage import Storage


class Command:
    def __init__(
        self,
        client: AsyncClient,
        store: Storage,
        config: Config,
        command: str,
        room: MatrixRoom,
        event: RoomMessageText,
    ):
        """A command made by a user.

        Args:
            client: The client to communicate to matrix with.

            store: Bot storage.

            config: Bot configuration parameters.

            command: The command and arguments.

            room: The room the command was sent in.

            event: The event describing the command.
        """
        self.client = client
        self.store = store
        self.config = config
        self.command = command
        self.event_room = room
        self.room = None
        self.event = event
        self.args = self.command.split()[1:]

    async def process(self):
        """Process the command"""
        
        # Ignore any commands from non-admins
        if self.event_room.power_levels.get_user_level(self.event.sender) != 100:
            return
        
        self.room = Room.get_existing(self.client, self.store, self.event_room.room_id)
        
        if not self.room:
            await send_text_to_room(self.client, self.event_room.room_id, "Room not initialized, initializing...")
            self.room = Room.create_new(self.client, self.store, self.event_room.room_id)
        
        if self.command.startswith("help"):
            await self._show_help()
        elif self.command.startswith("enable"):
            await self._enable()
        elif self.command.startswith("disable"):
            await self._disable()
        elif self.command.startswith("delay"):
            await self._delay()
        elif self.command.startswith("confirm"):
            await self._confirm()
        else:
            await self._unknown_command()

    async def _disable(self):
        """Disable message deletion"""
        
        if not self.room.deletion_turned_on:
            await send_text_to_room(self.client, self.event_room.room_id, "Deletion already disabled.")
            return
        
        self.room.deletion_turned_on = False
        
        if Destroyer.stop_room_loop(self.room):
            await send_text_to_room(self.client, self.event_room.room_id, "Deletion disabled.")
        else:
            await send_text_to_room(self.client, self.event_room.room_id, "Failed to disable room deletion.")
    
    async def _enable(self):
        """Request to enable message deletion"""
        
        if self.room.deletion_turned_on:
            await send_text_to_room(self.client, self.event_room.room_id, "Deletion turned on.")
            return
        
        if not self.room.delete_after_m:
            await send_text_to_room(self.client, self.event_room.room_id, "Message timeout not set. Set using `!c delay <delay in minutes>`")
            return
        
        first_event_id = await self.room.fetch_first_event_id()
        
        if not first_event_id:
            await send_text_to_room(self.client, self.event_room.room_id, "No messages will be deleted currently.")
        else:
            await send_text_to_room(self.client, self.event_room.room_id, "All messages above this message will be deleted.", reply_to_event_id=first_event_id)
        
        self.room.accept_requested = True
        await send_text_to_room(self.client, self.event_room.room_id, "To enable message deletion, please confirm with `!c confirm`")

    async def _confirm(self):
        """Request to enable message deletion confirmation"""
        if not self.room.accept_requested:
            await send_text_to_room(self.client, self.event_room.room_id, "Nothing to confirm.")
            return
        
        self.room.deletion_turned_on = True
        self.room.accept_requested = False
        
        if Destroyer.start_room_loop(self.room):
            await send_text_to_room(self.client, self.event_room.room_id, "Deleting old messages.")
        else:
            await send_text_to_room(self.client, self.event_room.room_id, "Failed to start deletion process.")

    async def _delay(self):
        """Delay command"""
        
        if len(self.args) > 1:
            await send_text_to_room(self.client, self.event_room.room_id, commands_help.COMMAND_DELAY)
            return
        elif len(self.args) == 1:
            await self._set_delay(self, self.args[0])
        elif len(self.args) == 0:
            await self._get_delay(self)
            
    async def _get_delay(self):

        if self.room.delete_after_m is None:
            text = "Delay not set. Set using `!c delay <delay in minutes>`"
        else:
            text = f"Messages are deleted after {self.room.delete_after_m//(60*1000)} minutes"
            
    async def _set_delay(self, delay: str):
        if not delay.isnumeric():
            await send_text_to_room(self.client, self.event_room.room_id, "Please enter a numeric delay value in minutes")
            return
        
        delay_minutes = int(delay)

        if delay_minutes <= 0:
            await send_text_to_room(self.client, self.event_room.room_id, "Delay must be positive")
            return    
        
        delay_ms = delay_minutes*60*1000
        
        self.room.set_delete_after(delay_ms)
  
    async def _echo(self):
        """Echo back the command's arguments"""
        response = " ".join(self.args)
        await send_text_to_room(self.client, self.event_room.room_id, response)

    async def _react(self):
        """Make the bot react to the command message"""
        # React with a start emoji
        reaction = "⭐"
        await react_to_event(
            self.client, self.event_room.room_id, self.event.event_id, reaction
        )

        # React with some generic text
        reaction = "Some text"
        await react_to_event(
            self.client, self.event_room.room_id, self.event.event_id, reaction
        )

    async def _show_help(self):
        """Show the help text"""
        if not self.args:
            text = (
                "Hello, I am bot destroyer. Use `help commands` to view "
                "available commands."
            )
            await send_text_to_room(self.client, self.event_room.room_id, text)
            return

        help_messages = {
            "commands":commands_help.AVAILABLE_COMMANDS,
            "enable":commands_help.COMMAND_ENABLE,
            "disable":commands_help.COMMAND_DISABLE,
            "delay":commands_help.COMMAND_DELAY,
        }
        topic = self.args[0]
        text = help_messages.get(topic,"Unknown help topic!")
        await send_text_to_room(self.client, self.event_room.room_id, text)

    async def _unknown_command(self):
        await send_text_to_room(
            self.client,
            self.event_room.room_id,
            f"Unknown command '{self.command}'. Try the 'help' command for more information.",
        )

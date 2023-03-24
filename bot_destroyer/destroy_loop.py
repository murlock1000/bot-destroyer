from __future__ import annotations
import asyncio
from typing import TYPE_CHECKING

from datetime import datetime
import logging
from nio import AsyncClient, RoomMessagesResponse, Event, RoomMessagesError, MessageDirection, RoomRedactError, RoomContextError, RoomContextResponse
from bot_destroyer.chat_functions import send_room_redact, send_text_to_room

from bot_destroyer.storage import Storage

logger = logging.getLogger(__name__)

persist_event_types = [
            "m.room.server_acl",
            "m.room.encryption",
            "m.room.name",
            "m.room.avatar",
            "m.room.topic",
            "m.room.guest_access",
            "m.room.history_visibility",
            "m.room.join_rules",
            "m.room.power_levels",
            "m.room.create",
            "m.room.member",
            "m.room.redaction",
            "default",
        ]

class IEvent(object):
    def __init__(self, event_id:str = None, timestamp:str = None, batch_token_start: str = None, batch_token_end: str = None):
        self.room_id = event_id
        self.timestamp = timestamp
        self.batch_token_start = batch_token_start
        self.batch_token_end = batch_token_end

class Room(object):
    
    room_cache = {}
    
    def __init__(self,
                 client: AsyncClient,
                 storage: Storage, 
                 room_id:str, 
                 ):
        
        self.client = client
        self.storage = storage
        
        # Fetch existing fields of Room
        fields = self.storage.get_room_all(room_id)
        
        self.room_id =            fields['room_id']
        
        self.last_event_id =      fields['event_id']
        self.timestamp =          fields['timestamp']
        self.batch_token_start =        fields['batch_token_start']
        self.batch_token_end =        fields['batch_token_end']
        
        self.deletion_turned_on = fields['deletion_turned_on']
        self.delete_after_m =     fields['delete_after']
        
        if self.delete_after_m is not None:
            self.delete_after_m = int(self.delete_after_m)
            
        if self.deletion_turned_on is not None:
            self.deletion_turned_on = self.deletion_turned_on == '1'
            
        if self.timestamp is not None:
            self.timestamp = int(self.timestamp)
        
        self.room = self.client.rooms.get(self.room_id, None)
        self.accept_requested = False
        
    def set_event(self, event_id: str, timestamp: str, batch_token_start: str, batch_token_end: str):
        self.last_event_id = event_id
        self.timestamp = timestamp
        self.batch_token_start = batch_token_start
        self.batch_token_end = batch_token_end
        
        self.storage.set_room_event(self.room_id, self.last_event_id, self.timestamp, self.batch_token_start, batch_token_end)
        
    def set_delete_after(self, delete_after_m):
        self.delete_after_m = delete_after_m
        self.storage.set_delete_after(self.room_id, delete_after_m)

    def event_expired(self, event: Event) -> bool:
        time_to_expiry = self.get_time_to_expiry_in_min(event.server_timestamp)
        
        return time_to_expiry < 0
    
    def get_time_to_expiry_in_min(self, timestamp:int) -> int:
        event_time = datetime.fromtimestamp(timestamp / 1000.0)
        current_time = datetime.now()
        
        return self.delete_after_m-(current_time - event_time).total_seconds()/60
        
    def enable_room_loop(self):
        self.deletion_turned_on = True
        self.accept_requested = False
        self.storage.set_deletion_turned_on(self.room_id, '1')
        
        return Destroyer.start_room_loop(self)
    
    def disable_room_loop(self):
        logger.debug("Disabling room loop")
        self.deletion_turned_on = False
        self.storage.set_deletion_turned_on(self.room_id, '0')
        
        return Destroyer.stop_room_loop(self)

    def event_is_redacted(self, ev: Event) -> bool:
        return False
    
    def event_redaction(self, ev:Event) -> bool:
        if ev.source.get("type", "default") == "m.room.redaction" and ev.source.get("sender", "") == self.client.user_id:
            return True
        return False
    
    def event_redacted(self, ev:Event) -> bool:
        if ev.source.get("redacted_because", {}).get("type", "default") == "m.room.redaction":
            return True
        return False
    
    def event_redacted_by_bot(self, ev:Event) -> bool:
        if self.event_redacted(ev) and ev.source.get("redacted_because", {}).get("sender", "default") == self.client.user_id:
            return True
        return False

    async def delete_previous_events(self) -> str:
        resp = RoomMessagesResponse("", [], None, self.batch_token_end)
        exit_loop = False   
        delete_from_block_token = None
        
        # Find first undeleted event by bot
        while(resp.start != resp.end and not exit_loop and resp.end is not None):
            resp = await self.client.room_messages(self.room_id, resp.end)
            if type(resp) == RoomMessagesError:
                logger.error(resp)
                raise Exception(resp.status_code)
            for ev in resp.chunk:
                
                if self.event_redacted_by_bot(ev):
                    delete_from_block_token = resp.end
                    exit_loop = True
                            
                if exit_loop:
                    break
        
        if delete_from_block_token is None:
            delete_from_block_token = resp.end
            
        resp.end = delete_from_block_token
        
        exit_loop = False
        while(resp.start != resp.end and not exit_loop and resp.end is not None):
            resp = await self.client.room_messages(self.room_id, resp.end, direction = MessageDirection.front)
            if type(resp) == RoomMessagesError:
                logger.error(resp)
                raise Exception(resp.status_code)
            
            for ev in resp.chunk:
                
                # Exit when found first event
                if ev.event_id == self.last_event_id:
                    exit_loop = True
                    break
                
                if self.event_redacted(ev):
                    continue
                if self.event_redaction(ev):
                    continue
                if ev.source.get("type", "default") in persist_event_types:
                    continue
                
                if self.event_expired(ev):
                    redact_resp = await send_room_redact(self.client, self.room_id, ev.event_id)
                    if type(redact_resp) == RoomRedactError:
                        logger.error(redact_resp)
                        raise Exception(redact_resp.status_code)
                else:
                    logger.error("Found non-expired messages before first message to be deleted.")
                    raise Exception("Found non-expired messages before first message to be deleted")
                if exit_loop:
                    break

    async def fetch_first_event_id(self) -> str:
        # Go over all events in the room (break if we find the first timed out event) and return the event id
        
        if not self.delete_after_m:
            return None
        
        resp = RoomMessagesResponse("", [], None, "")
        exit_loop = False
        iEvent = IEvent()
        
        while(resp.start != resp.end and not exit_loop and resp.end is not None):
            resp = await self.client.room_messages(self.room_id, resp.end)
            if type(resp) == RoomMessagesError:
                logger.error(resp)
                raise Exception(resp.status_code)
            for ev in resp.chunk:
                
                if self.event_redacted_by_bot(ev):
                    exit_loop = True
                elif self.event_redaction(ev):
                    continue
                else:
                    if ev.source.get("type", "default") not in persist_event_types:
                        if self.event_expired(ev):
                            iEvent.room_id = ev.event_id
                            iEvent.timestamp = ev.server_timestamp
                            iEvent.batch_token_start = resp.end
                            iEvent.batch_token_end = resp.start
                            exit_loop = True
                            
                if exit_loop:
                    break
        
        self.set_event(iEvent.room_id, iEvent.timestamp, iEvent.batch_token_start, iEvent.batch_token_end)
        return iEvent.room_id
    
    async def set_next_event(self):
        
        resp = RoomContextResponse(None, None, "", None, None, None, None)
        last_event_id = self.last_event_id
        exit_loop = False
        iEvent = IEvent()
        
        while(resp.start != resp.end and not exit_loop and resp.end is not None):
            
            resp = await self.client.room_context(self.room_id, last_event_id)
            if type(resp) == RoomContextError:
                logger.error(resp)
                raise Exception(resp.status_code)
            elif type(resp) == RoomContextResponse:
                if len(resp.events_after) == 0:
                    exit_loop = True
                    break

                for ev in resp.events_after:
                    last_event_id = ev.event_id
                    if ev.source.get("type", "default") not in persist_event_types:
                        iEvent.room_id = ev.event_id
                        iEvent.timestamp = ev.server_timestamp
                        iEvent.batch_token_start = resp.start
                        iEvent.batch_token_end = resp.end
                        exit_loop = True
                    if exit_loop:
                        break
                
        self.set_event(iEvent.room_id, iEvent.timestamp, iEvent.batch_token_start, iEvent.batch_token_end)
    
    async def main_loop(self):
        logger.debug("Starting loop...")
        await asyncio.sleep(2)
        
        # Delete all events before the starting event
        if self.last_event_id is not None:
            try:
                await self.delete_previous_events()
            except Exception as e:
                await send_text_to_room(self.client, self.room_id, f"Failed to delete events before last expired event with error: {e}")
                return
        
        while self.deletion_turned_on:
            
            if self.last_event_id:
                time_to_sleep_for_in_s = self.get_time_to_expiry_in_min(self.timestamp)*60
                if time_to_sleep_for_in_s > 0:
                    logger.debug(f"Room {self.room_id} sleeping for {time_to_sleep_for_in_s/60}m")
                    await asyncio.sleep(time_to_sleep_for_in_s)

                redact_resp = await send_room_redact(self.client, self.room_id, self.last_event_id)
                if type(redact_resp) == RoomRedactError:
                    await send_text_to_room(self.client, self.room_id, f"Failed to delete last expired event with error: {redact_resp}")
                    return
                try:
                    await self.set_next_event()
                except Exception as e:
                    await send_text_to_room(self.client, self.room_id, f"Failed to find next event with error: {e}")
                    return
            else:
                logger.debug("Waiting for default time")
                await asyncio.sleep(10)
                resp = None
                try:
                    resp = await self.fetch_first_event_id()
                except:
                    await send_text_to_room(self.client, self.room_id, f"Failed to fetch next event after none with error: {e}")
                    return
                
                if resp is not None:
                    try:
                        await self.delete_previous_events()
                    except Exception as e:
                        await send_text_to_room(self.client, self.room_id, f"Failed to delete events before next event after none with error: {e}")
                        return
                    
    @staticmethod
    def get_existing(client: AsyncClient, storage:Storage, room_id:str) -> Room:
        
        # Check cache first
        room = Room.room_cache.get(room_id, None)
        if room:
            return room
        
        # Find existing room in storage
        exists = storage.get_room(room_id)
        if not exists:
            return None
        else:
            room = Room(client, storage, room_id)
            Room.room_cache[room_id] = room
            return room

    @staticmethod
    def create_new(client: AsyncClient, storage:Storage, room_id:str) -> Room:
        # Create Room entry if not found in DB
        storage.create_room(room_id)
        return Room(client, storage, room_id)

class Destroyer(object):
    room_tasks = {}
    
    def __init__(self, client: AsyncClient, storage: Storage):
        self.client = client
        self.storage = storage
        
        room_ids = self.storage.get_all_rooms()
        
        for room_id in room_ids:
            room: Room = Room.get_existing(self.client, self.storage, room_id)
            
            if room.deletion_turned_on:
                main_loop = asyncio.get_event_loop()
                room_task = main_loop.create_task(room.main_loop())
                Destroyer.room_tasks[room_id] = room_task
    
    @staticmethod
    def start_room_loop(room: Room):
        if room.room_id in Destroyer.room_tasks.keys():
            logger.error(f"Room {room.room_id} already exists in task queue")
            return False
        
        main_loop = asyncio.get_event_loop()
        room_task = main_loop.create_task(room.main_loop())
        Destroyer.room_tasks[room.room_id] = room_task
        
        return True
    
    @staticmethod
    def stop_room_loop(room: Room):
        if room.room_id not in Destroyer.room_tasks.keys():
            logger.error(f"Room {room.room_id} is not in task queue")
            return False
        
        task = Destroyer.room_tasks.pop(room.room_id)
        was_canceled = task.cancel()
        
        return was_canceled
        
            
    
    
    


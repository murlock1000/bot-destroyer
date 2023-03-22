from __future__ import annotations
import asyncio
from typing import TYPE_CHECKING

from datetime import datetime
import logging
from nio import AsyncClient, RoomMessagesResponse, Event, RoomMessagesError, MessageDirection
from bot_destroyer.chat_functions import send_room_redact

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
        self.batch_token =        fields['batch_token']
        
        self.deletion_turned_on = fields['deletion_turned_on']
        self.delete_after_m =     fields['delete_after']
        
        if self.delete_after_m is not None:
            self.delete_after_m = int(self.delete_after_m)
        
        self.room = self.client.rooms.get(self.room_id, None)
        self.accept_requested = False
        
    def set_event(self, event_id: str, timestamp: str, batch_token: str):
        self.last_event_id = event_id
        self.timestamp = timestamp
        self.batch_token = batch_token
        
        self.storage.set_room_event(self.room_id, self.last_event_id, self.timestamp, self.batch_token)
        
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
        
        return Destroyer.start_room_loop(self)
    
    def disable_room_loop(self):
        logger.debug("Disabling room loop")
        self.deletion_turned_on = False
        
        return Destroyer.stop_room_loop(self)

    async def delete_previous_events(self) -> str:
        resp = RoomMessagesResponse("", [], None, self.batch_token)
        event_found = None
        
        # Find first undeleted event
        while(resp.start != resp.end and not event_found and resp.end is not None):
            resp = await self.client.room_messages(self.room_id, resp.end)
            if type(resp) == RoomMessagesError:
                logger.error(resp)
                return
            
            for ev in resp.chunk:
                if ev.source.get("redacted_because", {}).get("type", "default") == "m.room.redaction" and ev.redacter == self.client.user_id:
                    event_found = True
                    break
        
        event_found = False
        while(resp.start != resp.end and not event_found and resp.end is not None):
            resp = await self.client.room_messages(self.room_id, resp.end, direction = MessageDirection.front)
            if type(resp) == RoomMessagesError:
                logger.error(resp)
                return
            
            for ev in resp.chunk:
                # Exit when found first event
                if ev.event_id == self.last_event_id:
                    event_found = True
                    break
                
                if ev.source.get("redacted_because", {}).get("type", "default") == "m.room.redaction":
                    continue
                
                if self.event_expired(ev):
                    # If found previous redact events - stop                    
                    if ev.source.get("type", "default") not in persist_event_types:
                        try:
                            await send_room_redact(self.client, self.room_id, ev.event_id)
                        except Exception as e:
                            logger.error(f"Error: {e}")

    async def fetch_first_event_id(self) -> str:
        # Go over all events in the room (break if we find the first timed out event) and return the event id
        
        if not self.delete_after_m:
            return None
        
        resp = RoomMessagesResponse("", [], None, self.client.loaded_sync_token)
        event_found = None
        last_valid_ev = None
        
        while(resp.start != resp.end and not event_found and resp.end is not None):
            resp = await self.client.room_messages(self.room_id, resp.end)
            if type(resp) == RoomMessagesError:
                logger.error(resp)
                
                raise Exception(resp.status_code)
            for ev in resp.chunk:
                
                if ev.source.get("redacted_because", {}).get("type", "default") == "m.room.redaction":
                    event_found = last_valid_ev
                    break
                else:
                    if self.event_expired(ev):
                        if ev.source.get("type", "default") not in persist_event_types:
                            event_found = ev
                            break
                    elif ev.source.get("type", "default") not in persist_event_types:
                        last_valid_ev = ev
        
        if event_found:
            self.set_event(event_found.event_id, event_found.server_timestamp, resp.end)
            return event_found.event_id
        else:
            return None
    
    async def main_loop(self):
        logger.debug("Starting loop...")
        await asyncio.sleep(2)
        
        # Delete all events before the starting event
        await self.delete_previous_events()
        
        while self.deletion_turned_on:
            
            time_to_sleep_for_in_s = self.get_time_to_expiry_in_min(self.timestamp)*60
            
            if time_to_sleep_for_in_s > 0:
                logger.debug(f"Room {self.room_id} sleeping for {time_to_sleep_for_in_s/60}m")
                await asyncio.sleep(time_to_sleep_for_in_s)
            
            try:
                await send_room_redact(self.client, self.room_id, self.last_event_id)
            except Exception as e:
                logger.error(f"Error: {e}")
            
            resp = RoomMessagesResponse("", [], None, self.batch_token)
            event_found = None
            
            while(resp.start != resp.end and not event_found and resp.end is not None):
                resp = await self.client.room_messages(self.room_id, resp.end, direction = MessageDirection.front)
                if type(resp) == RoomMessagesError:
                    logger.error(resp)
                    return

                for ev in resp.chunk:
                    if ev.source.get("redacted_because", {}).get("type", "default") == "m.room.redaction":
                        continue
                    
                    if self.event_expired(ev):
                        # If found previous redact events - stop                    
                        if ev.source.get("type", "default") not in persist_event_types:
                            try:
                                await send_room_redact(self.client, self.room_id, ev.event_id)
                            except Exception as e:
                                logger.error(f"Error: {e}")
                    else:
                        event_found = True
                        self.set_event(ev.event_id, ev.server_timestamp, resp.start)
                        break

                
        
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
        
            
    
    
    


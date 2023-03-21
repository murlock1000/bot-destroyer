import datetime
from nio import AsyncClient, RoomMessagesResponse, Event

from bot_destroyer.storage import Storage

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
        
        self.last_event_id =      fields['last_event_id']
        self.timestamp =          fields['timestamp']
        
        self.deletion_turned_on = fields['deletion_turned_on']
        self.delete_after_m =       fields['delete_after']
        
        
        self.room = self.client.rooms.get(self.room_id, None)
        self.accept_requested = False

        
    def set_delete_after(self, delete_after):
        self.delete_after_m = delete_after
        self.storage.set_delete_after(self.room_id, delete_after)

    def event_expired(self, event: Event):
        event_time = datetime.fromtimestamp(event.server_timestamp / 1000.0)
        current_time = datetime.now()
        
        return (current_time - event_time)/60 > self.delete_after_m
        

    async def fetch_first_event_id(self) -> str:
        # Go over all events in the room (break if we find the first timed out event) and return the event id
        
        if not self.delete_after_m:
            return None
        
        resp = RoomMessagesResponse
        resp.end = self.client.loaded_sync_token
        resp.start = ""
        event_found = None
        
        while(resp.start != resp.end and not event_found):
            resp = await self.client.room_messages(self.room_id, resp.end)
            for ev in resp.chunk:
                if self.event_expired(ev):
                    if ev.type not in persist_event_types:
                        event_found = ev
                        break
        
        return ev
        
    @staticmethod
    def get_existing(client: AsyncClient, storage:Storage, room_id:str):
        
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
    def create_new(client: AsyncClient, storage:Storage, room_id:str):
        # Create Room entry if not found in DB
        storage.create_room(room_id)
        return Room(client, storage, room_id)

class Destroyer(object):
    
    def __init__(self, client: AsyncClient, storage: Storage):
        self.client = client
        self.storage = storage
        
        room_ids = self.storage.get_all_rooms()
        
        self.rooms = []
        
        for room_id in room_ids:
            room = Room.get_existing(self.storage, room_id)
            self.rooms.append(room)
    
    async def destroy_loop(client: AsyncClient, storage: Storage):
        pass
    


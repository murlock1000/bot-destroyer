import logging
from typing import Any, Dict, List

# The latest migration version of the database.
#
# Database migrations are applied starting from the number specified in the database's
# `migration_version` table + 1 (or from 0 if this table does not yet exist) up until
# the version specified here.
#
# When a migration is performed, the `migration_version` table should be incremented.
latest_migration_version = 2

logger = logging.getLogger(__name__)


class Storage:
    def __init__(self, database_config: Dict[str, str]):
        """Setup the database.

        Runs an initial setup or migrations depending on whether a database file has already
        been created.

        Args:
            database_config: a dictionary containing the following keys:
                * type: A string, one of "sqlite" or "postgres".
                * connection_string: A string, featuring a connection string that
                    be fed to each respective db library's `connect` method.
        """
        self.conn = self._get_database_connection(
            database_config["type"], database_config["connection_string"]
        )
        self.cursor = self.conn.cursor()
        self.db_type = database_config["type"]

        # Try to check the current migration version
        migration_level = 0
        try:
            self._execute("SELECT version FROM migration_version")
            row = self.cursor.fetchone()
            migration_level = row[0]
        except Exception:
            self._initial_setup()
        finally:
            if migration_level < latest_migration_version:
                self._run_migrations(migration_level)

        logger.info(f"Database initialization of type '{self.db_type}' complete")

    def _get_database_connection(
        self, database_type: str, connection_string: str
    ) -> Any:
        """Creates and returns a connection to the database"""
        if database_type == "sqlite":
            import sqlite3

            # Initialize a connection to the database, with autocommit on
            return sqlite3.connect(connection_string, isolation_level=None)
        elif database_type == "postgres":
            import psycopg2

            conn = psycopg2.connect(connection_string)

            # Autocommit on
            conn.set_isolation_level(0)

            return conn

    def _initial_setup(self) -> None:
        """Initial setup of the database"""
        logger.info("Performing initial database setup...")

        # Set up the migration_version table
        self._execute(
            """
            CREATE TABLE migration_version (
                version INTEGER PRIMARY KEY
            )
        """
        )

        # Initially set the migration version to 0
        self._execute(
            """
            INSERT INTO migration_version (
                version
            ) VALUES (?)
        """,
            (0,),
        )

        # Set up any other necessary database tables here

        logger.info("Database setup complete")

    def _run_migrations(self, current_migration_version: int) -> None:
        """Execute database migrations. Migrates the database to the
        `latest_migration_version`.

        Args:
            current_migration_version: The migration version that the database is
                currently at.
        """
        logger.debug("Checking for necessary database migrations...")

        if current_migration_version < 1:
            logger.info("Migrating the database from v0 to v1...")

            # Add new table, delete old ones, etc.
            # Add table for storing uploaded media file uris, so we don't have to reupload them to the server each time
            self._execute(
                """
                CREATE TABLE static_media_uris (
                    filename TEXT UNIQUE NOT NULL,
                    uri TEXT NOT NULL
                )
                """
            )
            # Update the stored migration version
            self._execute("UPDATE migration_version SET version = 1")

            logger.info("Database migrated to v1")
        if current_migration_version < 2:
            logger.info("Migrating the database from v1 to v2...")

            # Add new table, delete old ones, etc.
            # Add table for storing last to be destroyed events and their timestamps for rooms the bot is in.
            self._execute(
                """
                CREATE TABLE last_room_events (
                    room_id VARCHAR(80) PRIMARY KEY,
                    event_id VARCHAR(80),
                    timestamp TEXT,
                    delete_after TEXT,
                    deletion_turned_on VARCHAR(1),
                    batch_token_start VARCHAR(80),
                    batch_token_end VARCHAR(80)
                )
                """
            )
            # Update the stored migration version
            self._execute("UPDATE migration_version SET version = 2")

            logger.info("Database migrated to v2")

    def _execute(self, *args) -> None:
        """A wrapper around cursor.execute that transforms placeholder ?'s to %s for postgres.

        This allows for the support of queries that are compatible with both postgres and sqlite.

        Args:
            args: Arguments passed to cursor.execute.
        """
        if self.db_type == "postgres":
            self.cursor.execute(args[0].replace("?", "%s"), *args[1:])
        else:
            self.cursor.execute(*args)

    def delete_uri(self, filename: str):
        """Delete a uri entry via its filename"""
        self._execute(
            """
            DELETE FROM static_media_uris WHERE filename = ?
        """,
            ((filename,)),
        )

    def get_uri(self, filename):
        """Get the uri of a file by the filename"""

        self._execute(
            """
            SELECT uri FROM static_media_uris
            WHERE filename = ?
        """,
            ((filename,)),
        )

        row = self.cursor.fetchone()
        if row is not None:
            return row[0]
        return None

    def set_uri(self, filename, uri):
        """Create a new URI for a file with filename"""
        self._execute(
            """
            INSERT INTO static_media_uris (
                filename,
                uri
            ) VALUES(
                ?, ?
            )
        """,
            (
                filename,
                uri,
            ),
        )
        
    def create_room(self, room_id:str):
        self._execute(
            """
            INSERT INTO last_room_events (room_id) VALUES(?)
        """,
            (
                room_id,
            ),
        )
        
    def get_room(self, room_id:str) -> str:
        self._execute("SELECT room_id FROM last_room_events WHERE room_id= ?;", (room_id,))
        id = self.cursor.fetchone()
        if id:
            return id[0]
        return id
    
    def get_all_rooms(self) -> List[str]:
        self._execute("SELECT room_id FROM last_room_events;", ())
        rows = self.cursor.fetchall()
        return [row[0] for row in rows]
        
    def set_room_event(self, room_id:str, event_id:str, timestamp:str, batch_token_start:str, batch_token_end:str):
        self._execute(
            """
            UPDATE last_room_events SET event_id= ?, timestamp=?, batch_token_start=?, batch_token_end=? WHERE room_id =?
        """,
            (
                event_id,
                timestamp,
                batch_token_start,
                batch_token_end,
                room_id,
            ),
        )
        
    def set_delete_after(self, room_id:str, delete_after: str):
        self._execute(
            """
            UPDATE last_room_events SET delete_after= ? WHERE room_id =?
        """,
            (
                delete_after,
                room_id,
            ),
        )
        
    def set_deletion_turned_on(self, room_id:str, deletion_turned_on: bool):
        self._execute(
            """
            UPDATE last_room_events SET deletion_turned_on= ? WHERE room_id =?
        """,
            (
                deletion_turned_on,
                room_id,
            ),
        )
    
    def get_room_event(self, room_id:str):
        self._execute(
            """
            SELECT event_id, timestamp, batch_token_start, batch_token_end FROM last_room_events WHERE room_id =?
        """,
            (
                room_id,
            ),
        )
        
        row = self.cursor.fetchone()
        
        if row:
            return {
                "room_id": room_id,
                "event_id": row[0],
                "timestamp": row[1],
                "batch_token_start": row[2],
                "batch_token_end": row[3]
            }
        return None
    
    def get_room_all(self, room_id:str):
        self._execute(
            """
            SELECT room_id, event_id, timestamp, delete_after, deletion_turned_on, batch_token_start, batch_token_end FROM last_room_events WHERE room_id =?
        """,
            (
                room_id,
            ),
        )
        
        row = self.cursor.fetchone()
        
        if row:
            return {
                "room_id": row[0],
                "event_id": row[1],
                "timestamp": row[2],
                "delete_after": row[3],
                "deletion_turned_on": row[4],
                "batch_token_start": row[5],
                "batch_token_end": row[6]
            }
        return None
    
    def get_room_deletion(self, room_id:str):
        self._execute(
            """
            SELECT deletion_turned_on, delete_after FROM last_room_events WHERE room_id =?
        """,
            (
                room_id,
            ),
        )
        
        row = self.cursor.fetchone()
        
        if row:
            return {
                "room_id": room_id,
                "deletion_turned_on": row[0],
                "delete_after": row[1]
            }
        return None

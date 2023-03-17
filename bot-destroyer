#!/usr/bin/env python3
import asyncio
import sys

try:
    from bot_destroyer import main

    # Run the main function of the bot
    asyncio.get_event_loop().run_until_complete(main.main(sys.argv))
except ImportError as e:
    print("Unable to import bot_destroyer.main:", e)

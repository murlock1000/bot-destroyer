AVAILABLE_COMMANDS = """enable, disable, delay"""

COMMAND_ENABLE = """Replies to first message that will be deleted. After confirmation enables message deletion in a room after message expires in `delay` time. Usage:

`!c enable`
`!c confirm`
"""

COMMAND_DISABLE = """Disables message deletion after `delay` in a room. Usage:

`!claim disable`
"""

COMMAND_DELAY = """Show/Set delay in minutes after which messages will be deleted in a room. Usage:

Show delay:
`!c delay`

Set delay: 
`!c delay <delay in minutes>`
"""

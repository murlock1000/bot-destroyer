# bot-destroyer
[![Built with nio-template](https://img.shields.io/badge/built%20with-nio--template-brightgreen)](https://github.com/anoadragon453/nio-template)

A Matrix bot application that deleting messages from a chat older than a specified amount of time.

Available bot commands:
- `!c help`    - Shows help message for commands
- `!c delay 5` - sets delay to 5 minutes.
- `!c enable`  - enable room message deletion
- `!c confirm` - confirm the message deletion start (initialization only)
- `!c disable` - disable message deletion in room.

## Getting started

See [SETUP.md](SETUP.md) for how to setup and run the project.

## Usage
To enable the bot to delete messages, it must have message redaction power in a room (may be granted by setting power level to moderator). After setting the message deletion delay and enabling the deletion - bot will begin polling for messages and deleting expired ones.

## License

Apache2

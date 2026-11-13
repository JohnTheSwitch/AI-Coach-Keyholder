# Setting up your own AI Coach & Keyholder

This guide takes you from a fresh download to a running coach that is your Chaster keyholder. Everything runs on your own computer: a Discord bot,
a local language model (Ollama) and SQLite. Nothing leaves your machine except the Discord and Chaster calls.

At any point, `python -m cli setup check` shows what is set up and what is still missing (it never prints a secret).

## What you need

- A Windows, macOS or Linux computer that is on when you want the coach to work. Python 3.11 or newer (3.13/3.14 are used day to day).
- About 8 GB of free memory for the default model.
- A Discord account, and **two** Chaster accounts: your own (you wear the lock) and one for the coach (it holds the keys).

## 1. Get the code and install it

```bash
git clone https://github.com/JohnTheSwitch/AI-Coach-Keyholder.git
cd AI-Coach-Keyholder
python -m venv .venv
.venv\Scripts\activate          # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
```

On Windows, if `python` opens the Microsoft Store instead, see `windows/README.md` ("Interpreter detection").

## 2. The language model (Ollama)

Install Ollama from https://ollama.com, then:

```bash
ollama pull llama3.1
```

`OLLAMA_MODEL` in `.env` names the model (default `llama3.1`).

## 3. The Discord bot

1. https://discord.com/developers/applications -> New Application -> Bot.
2. Turn on **Message Content Intent** (Bot -> Privileged Gateway Intents).
3. Copy the token into `.env` as `DISCORD_TOKEN`.
4. Invite the bot to a server you are in (OAuth2 -> URL Generator -> scope `bot`, permissions to read and send messages). You talk to it by DM.
5. Set `APP_TIMEZONE` (for example `Europe/Prague`) and, if you want public holidays to count as days off, `HOLIDAY_COUNTRY` (for example `CZ`).

Start it once, so it creates its database: `python -m bot.discord_bot`. Send it a DM; the first-time onboarding starts.

## 4. Find your user id

```bash
python -m cli setup check
```

Under "Known user" you see an internal id with your Discord id. Put that internal id into `.env` as `OWNER_USER_ID` and, for the same person,
`CHECK_IN_USER_ID`. Restart the bot after every change to `.env`.

## 5. Chaster: the coach's keyholder account

1. Log in to Chaster as the **coach's** account. In the developer section, create an application.
   Its redirect URI must be exactly `http://localhost:8420/oauth/chaster/callback`.
2. Into `.env`: `CHASTER_CLIENT_ID`, `CHASTER_CLIENT_SECRET`, and a random `CHASTER_TOKEN_ENCRYPTION_KEY` (see the comment in `.env.example`).
   Keep `CHASTER_OAUTH_SCOPES=locks keyholder messaging profile offline_access`.
3. Restart the bot, DM it `chaster connect`, and log in **as the coach's account** in the browser that opens.
4. Set `CHASTER_EXECUTION_KEYHOLDER_USER_ID` to your `OWNER_USER_ID` (the internal id that ran `chaster connect`).

## 6. The lock

1. As the coach's account, create a **shared lock** (not a test lock): trusted keyholder, the **hygiene opening** extension, a short timer (the
   coach holds you with a freeze, not with time; it never adds or removes time). Give it a password so only you can join.
2. Join it with **your own** account.
3. In `.env`: `OWNER_CHASTER_ACCOUNT_ID` = your own Chaster account id, and `RELEASE_LOCK_ID`, `PENALTY_FREEZE_LOCK_ID`, `EXEMPTION_LOCK_ID` = `auto`.
   The coach then recognises your lock by itself, also when you join it again later.

## 7. Trust domains

```bash
python -m cli setup init --owner-id <your OWNER_USER_ID> --yes
```

This creates the Trust domains your configuration names (by default `routine` and `hygiene`), each with a consent record.

## 8. Let it act on the lock

A new install starts **halted**: nothing touches a lock until you clear it.

```bash
python -m scripts.execution_control status
python -m scripts.execution_control clear "set up and checked"
```

`python -m cli setup check` should now show nothing missing.

## 9. Keep it running

- Windows: `python -m scripts.make_start_script` writes `scripts/start_bot.bat` for your machine (it restarts the bot after a crash);
  `windows/install_task.ps1` starts it with Windows. See `windows/README.md`.
- macOS/Linux: run `python -m bot.discord_bot` under your service manager of choice (systemd, launchd).

Only ever run **one** bot instance.

## Safety, always

- Chaster's own emergency unlock stays available to you at all times. A health need or an emergency is never held against you.
- The coach only freezes and unfreezes. It never changes the lock's time and never unlocks it directly.
- `python -m scripts.execution_control halt "why"` stops every lock action at once.

## Updates

Once a day the bot tells you (by DM) when GitHub has a newer version. It never installs anything. To update:
`git pull`, `pip install -r requirements.txt`, restart.

## More

- `README.md`: the project's full history and architecture.
- `philosophy.md`: the principles every change must follow.
- `docs/operations/multi_wearer_runbook.md`: several wearers on one machine (switched off by default; not yet at full feature parity).

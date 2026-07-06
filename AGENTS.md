# AGENT.md
## 🤖 Batch Confession Bot Agent
This autonomous Python agent manages the lifecycle of anonymous student confessions: retrieving submissions from a Tally form, cleanly isolating the text payload, and broadcasting it securely to a designated Telegram batch channel.
## 🔄 System Workflow
The agent operates on a data pipeline that moves information from an anonymous web interface to a public Telegram broadcast channel.
```
[ Student ] ---> ( Tally.so Form Submission )
                         │
                         ▼
             [ Tally API Endpoint ]
                         │  (JSON Payload)
                         ▼
               ┌───────────────────┐
               │    Python Agent   │
               │  ──────────────── │
               │ 1. Fetch JSON     │
               │ 2. Parse Text     │
               │ 3. Filter Dups    │
               └───────────────────┘
                         │
                         ▼
             [ Telegram Bot API ]
                         │  (sendMessage)
                         ▼
       📢 [ Telegram Broadcast Channel ]

```
### Flow Breakdown
 1. Confession Bot AA user submits an anonymous confession via the Tally.so form frontend.
 2.anages the lifecycleThe Python agent periodically queries the Tally API (or receives data) to retrieve the raw structured submission JSON.
 3.sions: retrieving submissThe agent processes the nested JSON hierarchy (submission ➔ responses ➔ answer) to isolate the raw plaintext string.
 4.essions: retrieving submThe agent packages the plaintext string and dispatches it via an HTTP POST request to the Telegram Bot API endpoint (/sendMessage).
 5.ving submissions from aThe Telegram bot publishes the confession to the batch broadcast channel securely and anonymously.
## 🛠️ Data Extraction Logic
To preserve absolute anonymity and maintain performance, the agent bypasses form schema metadata and isolates only the textual response block using the following logic:w
The agent operates on a data pipeline that moves information from an anonymous web interface to a public Telegram broadcast channel.
```
[ Student ] ---> ( Tally.so Form Submission )
                         │
                         ▼
             [ Tally API Endpoint ]
                         │  (JSON Payload)
                         ▼
               ┌───────────────────┐
               │    Python Agent   │
           ## ⚙️ Environment Configuration
The agent requires the following system variables to be configured on the host server (VPS) to function:
| Variable | Description | Source |
|---|---|---|
| TALLY_API_URL | The endpoint URL for your specific Tally form data. | Tally Developer Dashboard |
| TALLY_API_KEY | Bearer token used to authenticate requests to Tally. | Tally Account Settings |
| TELEGRAM_BOT_TOKEN | Token generated for authorization. | @BotFather on Telegram |
| TELEGRAM_CHANNEL_ID | Unique identifier for your target broadcast channel. | Channel Info (e.g., @your_batch_confessions) |
## 🚀 VPS Deployment & Infrastructure
Designed to run efficiently on lightweight, low-resource virtual private servers (such as Azure for Students or Oracle Cloud Free Tier).
### Execution Lifecycle Options
 * 2. Parse Text     │
         Run as a continuous system service (systemd) that queries the Tally API at standard intervals, tracking the last processed submissionId in a local database or cache to avoid broadcast duplication.
 *ayload, and broadcasting it sTriggered every X minutes via a system crontab, processing unread entries and shutting down immediately to save memory.

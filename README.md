# scrapper

Two MCP servers that log in to Instagram and scrape it with Playwright, plus an LLM service that
keeps answering when a model is rate-limited, retired or down (one chat model walks an ordered
chain of Gemini and Groq models).

## Setup

```powershell
uv venv .venv --python 3.13
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv\Scripts\python -m playwright install chromium   # browser for the Instagram servers
copy .env.example .env        # fill in GEMINI_API_KEY, INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD

.venv\Scripts\python -m pytest -q                 # unit tests, no network
.venv\Scripts\python scripts\check_llm.py         # smoke test against the live LLM providers (--stream to test streaming)
.venv\Scripts\python scripts\check_instagram.py login   # log in once; then e.g. `profile natgeo`
```

## Instagram MCP servers (`app/servers`)

Two stdio MCP servers on top of Playwright that share one saved browser session:

| Server              | Module                          | Tools                                                                                                                                                              |
| ------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `instagram-login`   | `app.servers.instagram_login`   | `login`, `submit_two_factor_code`, `login_status`, `screenshot`, `logout`, `close_browser`                                                                         |
| `instagram-scraper` | `app.servers.instagram_scraper` | `get_profile`, `get_posts`, `get_post`, `get_comments`, `get_hashtag`, `search`, `fetch_api`, `get_page_text`, `screenshot`, `session_status`, `close_browser` |

`login` opens a visible Chromium window (so 2FA codes and "suspicious login" checkpoints can be
finished by hand), signs in with `INSTAGRAM_USERNAME` / `INSTAGRAM_PASSWORD`, and saves the browser
session to `data/instagram/session.json`. It returns `two_factor_required` when Instagram asks for a
code (answer with `submit_two_factor_code`) or `challenge_required` when it raises a checkpoint
(finish it in the window, then call `login_status`, which also refreshes the saved session).

The scraper runs headless, loads that file, and reloads it whenever the login server writes a new
one. It reads the JSON endpoints Instagram's own web app calls (`/api/v1/...`), fetched from inside
the logged-in page so they carry the browser's cookies and fingerprint. `fetch_api` exposes that
raw; `get_page_text` and `screenshot` are the fallback when an endpoint changes. Post tools accept a
shortcode or any `/p/`, `/reel/`, `/tv/` URL; paginate with the returned `next_max_id` / `next_min_id`.

`.mcp.json` registers both servers for Claude Code (project scope; approve them when prompted). For
Claude Desktop or another client, use absolute paths:

```json
{
  "mcpServers": {
    "instagram-login": {
      "command": "C:/path/to/scrapper/.venv/Scripts/python.exe",
      "args": ["-m", "app.servers.instagram_login"],
      "cwd": "C:/path/to/scrapper"
    },
    "instagram-scraper": {
      "command": "C:/path/to/scrapper/.venv/Scripts/python.exe",
      "args": ["-m", "app.servers.instagram_scraper"],
      "cwd": "C:/path/to/scrapper"
    }
  }
}
```

The same library runs without MCP from `scripts/check_instagram.py`
(`login`, `status`, `profile`, `posts`, `post`, `comments`, `hashtag`, `search`, `text`, `logout`).

Notes:

- Instagram's endpoints are undocumented and change without notice. The parsers are defensive and
  the raw tools stay available as a fallback.
- Keep the request volume modest. Instagram rate-limits (`RateLimited`) and may lock accounts that
  scrape aggressively; `INSTAGRAM_REQUEST_DELAY_SECONDS` paces every request. Use a dedicated
  account rather than a personal one.
- Scraping is against Instagram's Terms of Use. Stick to public data you are allowed to collect.

## LLM service (`app/llm`)

```python
from app.llm import get_llm

llm = get_llm()                                   # built once from .env
reply = llm.invoke("Extract the product name from: ...")
print(reply.text)                                 # Gemini 3 returns content blocks; .text flattens them
print(reply.response_metadata["llm_fallback"])    # {'provider': 'gemini', 'model': '...', 'name': 'gemini:...'}
```

`get_llm()` returns a normal LangChain chat model, so `invoke`, `ainvoke`, `stream`, `astream`,
`bind_tools(...)` and `with_structured_output(...)` all work and all fall back the same way.
`build_llm(temperature=0.7)` builds a separate instance for one call site.

Chain order (from `.env`):

```
LLM_PROVIDER models: *_MODEL, then *_FALLBACK_MODELS in order
  -> the other provider's models, if its API key is set
```

With the default `.env`:

```
gemini:gemini-3.5-flash-lite -> gemini:gemini-3.1-flash-lite -> gemini:gemini-3.5-flash
  -> groq:openai/gpt-oss-120b -> groq:openai/gpt-oss-20b -> groq:qwen/qwen3.8-27b
```

What happens on an error:

| Error                             | Action                                                                                         |
| --------------------------------- | ---------------------------------------------------------------------------------------------- |
| 429 / 5xx / timeout / connection  | retry `LLM_RETRIES_PER_MODEL` times, then next model; model skipped for `LLM_COOLDOWN_SECONDS` |
| 404 / unknown or deprecated model | next model; skipped for `LLM_UNAVAILABLE_COOLDOWN_SECONDS` (default 10 minutes)                |
| 401 / 403 / bad API key           | skip every model of that provider                                                              |
| 400 / 422 bad request             | raise immediately (the request itself is wrong, another model will not fix it)                 |
| everything failed                 | `AllModelsFailedError`, carrying every underlying exception                                    |

Cooled-down models are tried last rather than never, so the chain still answers when every model
has recently failed. Health is shared process-wide, so a 429 seen by one caller is remembered by
the next.

Streaming falls back only if a model fails before its first chunk; once tokens have reached the
caller the model cannot be switched.

## Configuration (.env)

| Variable                                          | Default  | Meaning                                            |
| ------------------------------------------------- | -------- | -------------------------------------------------- |
| `LLM_PROVIDER`                                    | `gemini` | Primary provider (`gemini` or `groq`)              |
| `GEMINI_API_KEY` / `GROQ_API_KEY`                 |          | A provider without a key is left out of the chain  |
| `GEMINI_MODEL` / `GROQ_MODEL`                     |          | First model of each provider                       |
| `GEMINI_FALLBACK_MODELS` / `GROQ_FALLBACK_MODELS` |          | Comma-separated, tried in order                    |
| `LLM_TEMPERATURE`                                 | `0`      | Applied to every model                             |
| `LLM_TIMEOUT_SECONDS`                             | `60`     | Per request, per model                             |
| `LLM_RETRIES_PER_MODEL`                           | `1`      | Attempts on the same model before moving on        |
| `LLM_COOLDOWN_SECONDS`                            | `30`     | Skip a model that just failed for this long        |
| `LLM_UNAVAILABLE_COOLDOWN_SECONDS`                | `600`    | Skip an unknown / retired model name for this long |
| `LANGSMITH_TRACING`                               | `false`  | Trace every call in LangSmith                      |

Instagram (read by `app/instagram/settings.py`):

| Variable                          | Default                       | Meaning                                                    |
| --------------------------------- | ----------------------------- | ---------------------------------------------------------- |
| `INSTAGRAM_USERNAME` / `PASSWORD` |                               | Used by the login server only                              |
| `INSTAGRAM_SESSION_FILE`          | `data/instagram/session.json` | Saved browser session shared by both servers               |
| `INSTAGRAM_LOGIN_HEADLESS`        | `false`                       | Hide the login browser (then 2FA must go through the tool) |
| `INSTAGRAM_HEADLESS`              | `true`                        | Hide the scraper browser                                   |
| `INSTAGRAM_LOGIN_WAIT_SECONDS`    | `45`                          | How long `login` waits for Instagram to accept the form    |
| `INSTAGRAM_NAV_TIMEOUT_SECONDS`   | `30`                          | Page navigation timeout                                    |
| `INSTAGRAM_REQUEST_DELAY_SECONDS` | `1.5`                         | Minimum (jittered) pause between requests to Instagram     |
| `INSTAGRAM_USER_AGENT`            | Chromium's own                | Override the browser user agent                            |

## Layout

```
app/
  config.py        Settings from .env (pydantic-settings)
  llm/
    errors.py      Classifies provider exceptions: transient / model unavailable / auth / fatal
    fallback.py    FallbackChatModel, FallbackPolicy, HealthTracker
    factory.py     build_llm() / get_llm() from Settings
  instagram/
    settings.py    INSTAGRAM_* settings
    browser.py     BrowserSession: lazy Chromium + one tab, session file load/save, request pacing
    auth.py        Login flow: password form, 2FA, checkpoints, logout
    api.py         Web-API reads (profile, posts, post, comments, hashtag, search) + parsers
    codec.py       Shortcode <-> media id, username / shortcode from URLs
    runtime.py     SharedSession: one browser per server process, one tool call at a time
  servers/
    instagram_login.py    MCP server: login / 2FA / status / logout
    instagram_scraper.py  MCP server: scraping tools
.mcp.json              Registers both servers for Claude Code
scripts/check_llm.py        Live LLM smoke test
scripts/check_instagram.py  Drive the Instagram library from the command line
tests/                 Unit tests (no network, no browser)
```

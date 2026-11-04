# scrapper

Two MCP servers that log in to Instagram and scrape it with Playwright, an LLM service that
keeps answering when a model is rate-limited, retired or down (one chat model walks an ordered
chain of Gemini and Groq models), and an agent that joins the two: ask in plain words which
accounts post, comment and like under some hashtags and it picks the lookups and answers. A second
agent maps related hashtags hashtag by hashtag into an embedded graph database (LadybugDB).

## Setup

```powershell
uv venv .venv --python 3.13
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv\Scripts\python -m playwright install chromium   # browser for the Instagram servers
copy .env.example .env        # fill in GEMINI_API_KEY, INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD

.venv\Scripts\python -m pytest -q                 # unit tests, no network
.venv\Scripts\python scripts\check_llm.py         # smoke test against the live LLM providers (--stream to test streaming)
.venv\Scripts\python scripts\check_instagram.py login   # log in once; then e.g. `profile natgeo`
.venv\Scripts\python -m app.agent "who posts under #ceylontea and who comments on their posts?"
.venv\Scripts\python -m app.web                   # the web UI, with the same agent as a chat panel
```

## Instagram MCP servers (`app/servers`)

Two stdio MCP servers on top of Playwright that share one saved browser session:

| Server              | Module                          | Tools                                                                                                                                                              |
| ------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `instagram-login`   | `app.servers.instagram_login`   | `login`, `submit_two_factor_code`, `login_status`, `screenshot`, `logout`, `close_browser`                                                                         |
| `instagram-scraper` | `app.servers.instagram_scraper` | `get_profile`, `get_posts`, `get_post`, `get_comments`, `get_likers`, `get_post_engagement`, `get_hashtag`, `get_hashtag_users`, `get_hashtag_engagement`, `search`, `fetch_api`, `get_page_text`, `screenshot`, `session_status`, `close_browser` |

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

Hashtags work differently: Instagram retired the hashtag JSON endpoint and now redirects
`/explore/tags/<tag>/` to its keyword-search page, which fills its grid through GraphQL on scroll.
`get_hashtag` and `get_hashtag_users` open that page like a user, scroll it, and parse the responses the
page itself receives (about 24 posts per scroll, owner included), so the request pattern is exactly
the web app's own. `get_hashtag_users` takes several hashtags (`"tea, #coffee"` or a list) and returns
the merged, deduplicated accounts with the hashtags each was seen under.

Profiles work the same way (`app/instagram/profiles.py`). The JSON profile endpoint
(`/api/v1/users/web_profile_info/`) answers HTTP 429 for this account while the profile page itself
loads fine, so `get_profile` opens `instagram.com/<username>/` and parses the page's own
`PolarisProfilePageContentQuery` (name, bio, bio links, follower / following / post counts, business /
private / verified flags, city) and `PolarisProfilePostsQuery` (the 12 newest posts, collab posts
included with their `coauthors`; each scroll adds 12 more). It falls back to the JSON endpoint only when
the page loads without that data, and raises `ProfileNotFound` for an account that does not exist.
`get_posts` reads the same page unless you pass a `max_id` cursor.

Engagement: `get_likers` returns the first hundred or so likers of a post plus the total (Instagram's
web endpoint pages no further); `get_post_engagement` adds the comments and one row per commenter;
`get_hashtag_engagement` runs the hashtag crawl and then the engagement of the first N posts in one
call, returning the posters, the posts and the merged `people` (commenters and likers with the posts
each engaged with). Every post costs about two requests, so keep the post count modest; when Instagram
starts throttling the crawl stops early, keeps what it has, and says so in `message`.

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
(`login`, `status`, `profile`, `posts`, `post`, `comments`, `likers`, `engagement`, `hashtag`,
`hashtag-users`, `search`, `text`, `logout`).

Notes:

- Instagram's login form changed in 2026 (`email` / `pass` fields, a hidden submit input); `auth.py`
  matches both the old and the new markup. A fresh login usually triggers an e-mail code; answer it
  with `submit_two_factor_code`.
- Instagram's endpoints are undocumented and change without notice. The parsers are defensive and
  the raw tools stay available as a fallback.
- Keep the request volume modest. Instagram rate-limits (`RateLimited`) and may lock accounts that
  scrape aggressively; `INSTAGRAM_REQUEST_DELAY_SECONDS` paces every request. Use a dedicated
  account rather than a personal one.
- Scraping is against Instagram's Terms of Use. Stick to public data you are allowed to collect.

## Web UI (`app/web`)

```powershell
.venv\Scripts\python -m app.web          # prints the URL; http://127.0.0.1:8000, or the next free port
.venv\Scripts\python -m app.web --port 8080   # pick the port yourself
```

An Instagram-style page for the hashtag crawl. Type hashtags (Enter, space or comma separate them), pick
how many posts per hashtag and how far to scroll, press **Scrape**. Progress streams in while the crawl
runs; then the page shows the posts as a grid (click one for caption, counts, date and the link) and the
accounts behind them as a list (verified / private flags, posts found, the hashtags each was seen under,
link to the profile). Then press **Fetch engagement** to pull the commenters and likers of the first N
posts: every post gains its comments and its likers (open a post to read them), the accounts list gains
the commenters and likers with Posters / Commenters / Likers filters, and the exports include the roles.
Copy the usernames or download CSV / JSON. It uses the same saved session and headless browser as the
scraper server and runs one crawl at a time.

Every account row has a **Profile** button: it loads the profile (name, followers, following, posts,
business / city, bio, links) into a card under the row, and the CSV export gains those columns for every
profile loaded. `GET /api/profile?username=...` is the same lookup as JSON.

The **Ask the agent** panel at the top is the same page driven by the agent below: type a question
("who posts under #tea and #ceylontea, and who comments on their posts most?") and watch it work. Each
question gets a collapsible trace laid out like a LangSmith trace: a tree of runs, each with a type
badge (**Chain**, **LLM**, **Tool**), its timing, a latency bar, and expandable **Input** / **Output**.
The question is the root chain; every model call is an LLM run showing the model that answered, its
tokens, its reasoning and which tools it asked for (its input lists the messages it was sent); the tool
calls of one turn sit in a `tools` chain, one Tool run each with its arguments, a one-line outcome and
the exact JSON the model read. Click a run to fold it. The answer follows the trace. Whatever it crawls also fills the posts grid and the accounts
list (profiles it looks up open as cards there), so the filters and exports work on the agent's results
too. The panel shows which model answers (and the LangSmith project, when tracing is on); without an
LLM API key in `.env` it stays disabled and the manual flow still works.

## Agent (`app/agent`)

```powershell
.venv\Scripts\python -m app.agent "which accounts post under #ceylontea? rank them"
.venv\Scripts\python -m app.agent --steps 4 "find hashtags related to ceylon tea and who posts under the biggest one"
```

```python
from app.agent import run_agent
from app.instagram import SharedSession, get_instagram_settings

shared = SharedSession(get_instagram_settings(), headless=True)
result = await run_agent("who likes the posts under #tea?", acquire=shared.acquire, on_event=print)
print(result["answer"], result["model"], result["tool_calls"])
```

Plain LangChain tool calling on top of `get_reasoning_llm()`: the same Gemini / Groq fallback chain as
`get_llm()`, with the models asked to return their reasoning too (Gemini gets a thinking budget of
`LLM_THINKING_BUDGET` tokens and returns a summary of its thoughts; Groq's gpt-oss / qwen3 return theirs
anyway). If a model rejects that request, the run carries on with the plain chain. The model gets six tools, each a thin wrapper over `app/instagram`:

| Tool               | What it does                                                           | Cost                     |
| ------------------ | ---------------------------------------------------------------------- | ------------------------ |
| `search_hashtags`  | Posts under hashtags + the accounts posting them (`hashtags.py`)       | 1 + scrolls per hashtag  |
| `get_engagement`   | Commenters and likers of posts, merged per person (`engagement.py`)    | ~2 per post, max 30 posts|
| `get_profile`      | Name, bio, links, counts, flags, newest posts (`profiles.py`)          | 1 profile page view      |
| `get_user_posts`   | Newest posts of one account (`profiles.py`)                            | 1 page + 1 scroll per 12 |
| `get_post`         | One post by shortcode or URL                                           | 1                        |
| `search_instagram` | Instagram search: accounts, hashtags with post counts, places          | 1                        |

What makes it safe to point at a real account: the system prompt tells the model to fetch only what
the question needs (24-48 posts per hashtag by default, 10-20 posts per engagement call), every tool
caps its arguments, a run gets at most `MAX_STEPS` (8) tool-calling turns, and the first `RateLimited`
or `LoginRequired` from Instagram ends the run: remaining calls of that turn are skipped and the model
is asked to answer with what it has and say so. The model only sees a compact view of each result
(captions trimmed, lists capped, media URLs dropped); the full result travels as the tool message's
`artifact`, which is what the web UI renders. Tool failures (unknown tool, bad arguments, a deleted
post) go back to the model as error messages rather than ending the run.

Each run is a tree of LangChain runs typed the way LangSmith types them (`app/agent/trace.py`):

```
chain  Hashtag Scout agent          the question
├─ llm    gemini:…  step 1          model call: reasoning, text, tool calls, token usage
├─ chain  tools     step 1          the tool calls of that turn
│  ├─ tool  search_hashtags         arguments → summary, the output the model read, full result
│  └─ tool  get_engagement
└─ llm    gemini:…  step 2          writes the answer
```

The same run ids go to LangChain's callback system, so with `LANGSMITH_TRACING=true` LangSmith
records one trace per question with exactly this tree (tagged `hashtag-scout`). A model call that fails
shows up as a failed LLM run, followed by the retry.

Events (`on_event`): `run_start` (`run_id`, `parent_id`, `run_type` chain / llm / tool, `name`, `step`,
`offset` seconds into the run, `inputs`), `run_end` (the same ids, `seconds`, `error`, plus `outputs` for
a chain; `model`, `reasoning`, `text`, `tool_calls`, `usage` for an llm run; `summary`, `output`,
`result` for a tool run), then `final` (answer, model used, turns, tool calls, tokens, seconds, why it
stopped if the loop ended it). `python -m app.agent` prints the same tree as it grows.
`POST /api/agent` on the web server streams exactly these as server-sent events; `history` in the request body replays earlier chat turns (answers only, no tool
traces) so follow-up questions work.

Scraping, the manual web flow and the MCP servers make no LLM calls at all; the agent is the one
place a model is involved. (Driving the MCP scraper from Claude Code is the same idea with Claude
as the agent.)

## Hashtag map (`app/graph`, `app/agent/mapper.py`)

The web UI's **Hashtag map** tab (also `http://127.0.0.1:8000/#map`) takes one to three seed hashtags,
e.g. `concertslk`, and lets an agent map the hashtags around them into a graph database on this machine:

1. it searches the seed and reads the other hashtags in the captions of those posts;
2. it adds the most frequent related ones as nodes (about node limit / searches per search), skipping
   generic tags like `#love` or `#instagood`;
3. then it goes hashtag by hashtag: each hashtag it searches links to the hashtags already in the graph
   and brings new candidates. The graph stops growing at the node limit (50 by default); searches left
   over (10 by default) go to the unexplored nodes with the highest counts, adding links between them.

The model decides what is on topic and what to search next, with two tools: `explore_hashtag` (one
Instagram search, about 24 posts, one or two requests) and `add_hashtags` (admit candidates; no
requests). It cannot add a hashtag no searched post carried, cannot search a hashtag twice, and stops
on throttling like the Q&A agent. The run is the same loop as the Q&A agent (`run_agent`), so its trace
shows the same chain / LLM / tool runs, and LangSmith gets it tagged `hashtag-map`.

What the numbers mean:

- **count** of a hashtag: how many collected posts carry it, counting only posts found while searching
  some *other* hashtag (its own search would trivially return it). Node size and the number in it.
- **edge** `A -> B`: posts found by searching `#A` also carry `#B`; its count is how many of them. The
  page draws A -> B and B -> A as one line, its width the sum.
- **searched**: the hashtag's own posts were read, so its outgoing links are known.

The page draws the graph live as the agent works (drag nodes, drag to pan, scroll to zoom, hover to light
up a hashtag's links, click for the list), lists every node with its count, and keeps the agent's trace and
summary next to it. Every map is saved after each step; the saved-maps list reloads or deletes one, and
JSON downloads it. Building the same seeds again replaces that map.

The graph database is [LadybugDB](https://ladybugdb.com) (the maintained fork of Kuzu): embedded, files under
`GRAPH_DB_PATH` (`data/hashtag_graph`), queried with Cypher, no server to run. Schema:

```
(:HashtagMap {id, seeds, max_nodes, max_searches, status, summary, searches, posts, created_at, updated_at})
    -[:INCLUDES {count, seed, explored, posts_read, depth, found_via, degree, ord}]->(:Hashtag {tag})
(:Hashtag)-[:CO_OCCURS {map, count}]->(:Hashtag)
```

A hashtag is one node whatever maps include it, so maps can be compared in Cypher, e.g.

```python
import real_ladybug as lb
conn = lb.Connection(lb.Database("data/hashtag_graph"))
conn.execute("MATCH (m:HashtagMap)-[r:INCLUDES]->(h:Hashtag) RETURN h.tag, collect(m.id), sum(r.count) ORDER BY sum(r.count) DESC LIMIT 20")
```

LadybugDB locks its files while a process has them open; the app opens the database per operation, so
close any such session before building a map. HTTP: `POST /api/map` (server-sent events: the run tree
plus a `graph` snapshot after every change), `GET /api/maps`, `GET` / `DELETE /api/maps/{id}`.

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
| `LLM_THINKING_BUDGET`                             | `1024`   | Gemini thinking tokens when the agent shows reasoning (0 = off) |
| `LANGSMITH_TRACING`                               | `false`  | Trace every call in LangSmith                      |
| `GRAPH_DB_PATH`                                   | `data/hashtag_graph` | Graph database holding the hashtag maps |

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
  agent/
    tools.py       The six Instagram tools (argument schemas, compact views for the model, full results as artifacts)
    runner.py      run_agent(): tool-calling loop with a step budget and a stop on throttling; describe()
    trace.py       The run tree (chain / llm / tool runs), mirrored to LangChain callbacks and LangSmith
    mapper.py      run_mapper(): the hashtag-mapping agent (explore_hashtag / add_hashtags over one map)
  graph/
    model.py       HashtagGraph: hashtags from captions, counts, edges, candidates, snapshot
    store.py       GraphStore: the maps in LadybugDB (save / load / list / delete), Cypher
    __main__.py    python -m app.agent "question"
  instagram/
    settings.py    INSTAGRAM_* settings
    browser.py     BrowserSession: lazy Chromium + one tab, session file load/save, request pacing
    auth.py        Login flow: password form, 2FA, checkpoints, logout
    api.py         Web-API reads (profile, posts, post, comments, search) + parsers
    hashtags.py    Hashtag -> posts / accounts, read off the search page the way the web app loads it
    engagement.py  Commenters and likers per post, merged into one list of people
    profiles.py    Profile + newest posts, read off the profile page the way the web app loads it
    codec.py       Shortcode <-> media id, username / shortcode from URLs
    runtime.py     SharedSession: one browser per server process, one tool call at a time
  servers/
    instagram_login.py    MCP server: login / 2FA / status / logout
    instagram_scraper.py  MCP server: scraping tools
  web/
    server.py      Starlette app behind the web UI: streaming crawl, agent and map endpoints, image proxy
    static/        The page itself (index.html, styles.css, app.js, graph.js: the map's force layout in SVG)
.mcp.json              Registers both servers for Claude Code
scripts/check_llm.py        Live LLM smoke test
scripts/check_instagram.py  Drive the Instagram library from the command line
tests/                 Unit tests (no network, no browser)
```

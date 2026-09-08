# fbmonitor — read-only Facebook & Instagram inbox monitor

Reports new comments, direct messages, visitor posts and recommendations
across one or more businesses, straight from the Meta Graph API.

**It never writes.** There is no code path in this package that can post a
comment, send a message or change anything on a Page — the Graph client
exposes only `get`, and a test asserts that stays true. You read the digest
and decide what to do.

## Why not go through a publishing tool

Publishing tools only see comments on posts *they* published. That misses
posts made directly in Meta Business Suite, comments on ads (especially
dark posts, which never appear in the Page feed at all), visitor posts and
recommendations. Reading Graph directly covers all seven surfaces:

| Source | Kind | Graph edge |
|---|---|---|
| Comments on Page posts | `page_comment` | `/{page-id}/posts` → `comments` |
| Comments on ads (incl. dark posts) | `ad_comment` | `/{ad-account}/ads` → `effective_object_story_id` → `comments` |
| Comments on Instagram posts | `instagram_comment` | `/{ig-user-id}/media` → `comments` |
| Messenger DMs | `messenger_dm` | `/{page-id}/conversations?platform=messenger` |
| Instagram DMs | `instagram_dm` | `/{ig-user-id}/conversations?platform=instagram` |
| Visitor posts on the Page | `visitor_post` | `/{page-id}/visitor_posts` |
| Page recommendations | `review` | `/{page-id}/ratings` |

Your own replies are filtered out of DM threads, so an answered enquiry
does not keep showing up as something needing attention.

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Create a Meta app and get a Page token

1. At [developers.facebook.com](https://developers.facebook.com/apps) create
   an app of type **Business**.
2. Add the **Facebook Login** and **Instagram** products.
3. In the [Graph API Explorer](https://developers.facebook.com/tools/explorer),
   select your app and request these **read** permissions:

   | Permission | Needed for |
   |---|---|
   | `pages_show_list` | finding your Pages |
   | `pages_read_engagement` | comments, recommendations |
   | `pages_read_user_content` | visitor posts, others' comments |
   | `pages_messaging` | reading Messenger threads |
   | `instagram_basic` | Instagram media |
   | `instagram_manage_comments` | Instagram comments |
   | `instagram_manage_messages` | Instagram DMs |
   | `ads_read` | comments on ads |

   `pages_messaging` and the two `instagram_manage_*` scopes are the ones
   that usually need App Review before they work on a live Page. Everything
   else keeps working while that is pending — a missing scope is reported
   as "not checked", not as a crash.

4. Generate a **Page access token** (not a user token), then exchange it for
   a long-lived one — short-lived tokens expire in about an hour:

   ```bash
   curl -s "https://graph.facebook.com/v21.0/oauth/access_token?\
   grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET\
   &fb_exchange_token=SHORT_LIVED_TOKEN"
   ```

   A long-lived Page token does not expire as long as it is used, but it is
   still a credential: keep it out of git.

### 3. Find your IDs

```bash
# Page IDs, and the linked Instagram account for each
curl -s "https://graph.facebook.com/v21.0/me/accounts\
?fields=id,name,instagram_business_account&access_token=$TOKEN"

# Ad accounts
curl -s "https://graph.facebook.com/v21.0/me/adaccounts\
?fields=id,name&access_token=$TOKEN"
```

### 4. Configure

```bash
cp accounts.example.yaml accounts.yaml   # fill in IDs
cp .env.example .env                     # fill in tokens
```

Tokens are never written into `accounts.yaml` — each account names an
environment variable instead, read at run time. Both `accounts.yaml` and
`.env` are gitignored.

## Use

```bash
set -a && source .env && set +a          # load tokens

python3 -m fbmonitor                     # digest of everything new
python3 -m fbmonitor --preview           # look, but don't mark as seen
python3 -m fbmonitor -f json             # machine-readable
python3 -m fbmonitor --source messenger_dm --source instagram_dm
python3 -m fbmonitor --account republic-of-barbers
```

The first real run reports the whole visible backlog, which is noisy once.
Use `--preview` to see it without consuming it. Every run after that reports
only what is new, tracked in `state.json`.

Exit codes: `0` clean, `1` something could not be checked (bad token,
missing scope), `2` config error. A scheduled run can key off `1` so a
broken token surfaces instead of looking like a quiet day.

### Scheduling

```cron
*/15 * * * * cd /path/to/facebook-monitor && \
  set -a && . ./.env && set +a && \
  /usr/bin/python3 -m fbmonitor >> digest.log 2>&1
```

Graph has no webhook in this design, so monitoring means polling. Every 15
minutes sits well inside the rate limits for a couple of Pages.

## Notes

- **Graph API version.** Defaults to `v21.0` (`fbmonitor/graph.py`). Meta
  retires versions roughly two years after release, so check the
  [changelog](https://developers.facebook.com/docs/graph-api/changelog) and
  bump it — either edit the constant or pass `--api-version`.
- **Ad comments need the right token.** Comments on an ad live on the Page
  post behind it, so the token must be a Page token for the Page the ads run
  under. If it is not, the collector says so rather than reporting nothing.
- **Recommendations have no stable ID** on every entry, so one is derived
  from the reviewer and timestamp to keep deduplication working.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

30 tests, no network — Graph is faked at the client seam.

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
| **Comments on ads, both placements** | `ad_comment` | `/{ad-account}/ads` → `effective_object_story_id` *and* `effective_instagram_media_id` → `comments` |
| Comments on Page posts | `page_comment` | `/{page-id}/posts` → `comments` |
| Comments on Instagram posts | `instagram_comment` | `/{ig-user-id}/media` → `comments` |
| Messenger DMs | `messenger_dm` | `/{page-id}/conversations?platform=messenger` |
| Instagram DMs | `instagram_dm` | `/{ig-user-id}/conversations?platform=instagram` |
| Visitor posts on the Page | `visitor_post` | `/{page-id}/visitor_posts` |
| Page recommendations | `review` | `/{page-id}/ratings` |

Your own replies are filtered out of DM threads, so an answered enquiry
does not keep showing up as something needing attention.

### Ad comments get priority

A comment on an organic post is seen by whoever happens to visit. A comment
under a *running ad* is served to every future person that ad reaches, so a
hostile one costs money for as long as it stands. Ad comments therefore lead
the digest, and each item is scored:

| | Meaning |
|---|---|
| `‼` | possible complaint — shown first, above everything newer |
| `⚠` | an unanswered question, i.e. a lead going cold |
| `•` | everything else |

The scoring is a keyword heuristic (`fbmonitor/triage.py`), not a sentiment
model. It **orders** the digest and never filters it — every item collected
is always shown. Treat a `‼` as "look here first" and its absence as no
information at all; tune the word lists to how your customers actually
complain.

### Verified account map

Two businesses are in scope, and both Pages sit in the **Republic Of
Barbers** business portfolio (`835665240453786`), so a single Meta app
connected to that portfolio covers both:

| Page | ID |
|---|---|
| Republic of Barbers est 2021 | `106828815133188` |
| Raising Thinkers | `1059125853953466` |

Every ad account was checked for which Page it actually promotes, rather
than inferred from its name — two names are actively misleading:

| Ad account | Promotes | In scope |
|---|---|---|
| `3178575389134669` Republic Of Barbers | Republic of Barbers | yes |
| `1192428111245965` Rob Barbers | Republic of Barbers + one out-of-scope Page | yes |
| `548901053966634` SC - Marketing | **Raising Thinkers** | yes |
| `886036749311933` ROB Marketing Agency | **Fitfable** — a different business | no |
| SC1, SC2, RC - Agency, RC - Marketing 2 | — | no ads ever |
| `721934336045857` SC3 - Marketing Agency | unknown | not checkable |
| `869845057383261` Richard Marketing | unknown | account UNSETTLED |

`Rob Barbers` promotes a second Page outside this scope. Those ads' comments
are skipped rather than erroring: a Page token cannot read another Page's
posts, and a partial read is not treated as a failure. The four dormant
accounts are excluded so they do not cost calls on every run.

### One ad has two comment threads### One ad has two comment threads

An ad running Advantage+ placements appears on both Facebook and Instagram,
and each carries a **separate** thread. Reading only the Facebook side
silently loses every Instagram comment on the same ad, so both IDs on the
creative are walked. A business also commonly runs ads from several ad
accounts at once (in-house plus an agency's), so `ad_account_ids` takes a
list and one unreadable account does not lose the others.

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Create a Meta app and get a Page token

1. **Register as a Meta developer first.** Landing on
   developers.facebook.com while signed in to Facebook shows marketing
   pages, not a dashboard — there is no "Create App" button until you have
   registered, which is the usual reason people get stuck here. Go straight
   to
   [developers.facebook.com/async/registration](https://developers.facebook.com/async/registration),
   accept the terms, and confirm the codes sent to your phone and email.
   It is free and takes a couple of minutes.

2. **Create the app** at
   [developers.facebook.com/apps/creation](https://developers.facebook.com/apps/creation/)
   (or **My Apps → Create App**). Give it a name and a contact email.

   When asked to pick a **use case**, choose **Other**, then app type
   **Business**. The guided use cases pre-select a narrow permission set;
   "Other" is what leaves you free to add all eight permissions below,
   which span Pages, Instagram and ads.
3. In the [Graph API Explorer](https://developers.facebook.com/tools/explorer),
   select your app from the dropdown and request these **read**
   permissions:

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

   **You probably do not need App Review.** While the app is in
   *Development* mode, its permissions work for anyone who holds a role on
   the app (admin, developer, tester). You are an admin of your own app and
   of these Pages, so reading your own Pages' data works straight away. App
   Review only becomes necessary if you switch the app to Live mode or need
   it to act for people who have no role on it. If a permission does get
   refused, the run reports that source as "not checked" and the others
   carry on regardless.

4. **Get a long-lived token, in this order.** The order matters: a Page
   token inherits the lifetime of the user token it came from, so deriving
   one from a short-lived user token gives you a Page token that dies in
   about an hour.

   a. In the Explorer, generate a **User** token with the permissions above
      (this one is short-lived — that is fine, it is only a stepping stone).

   b. Exchange it for a **long-lived user token** (~60 days). App ID and
      secret are in your app's Settings → Basic:

      ```bash
      curl -s "https://graph.facebook.com/v26.0/oauth/access_token\
      ?grant_type=fb_exchange_token\
      &client_id=APP_ID&client_secret=APP_SECRET\
      &fb_exchange_token=SHORT_LIVED_USER_TOKEN"
      ```

   c. Use that long-lived user token to ask for your **Page tokens**:

      ```bash
      curl -s "https://graph.facebook.com/v26.0/me/accounts\
      ?fields=id,name,access_token,instagram_business_account\
      &access_token=LONG_LIVED_USER_TOKEN"
      ```

      Each Page in the response carries its own `access_token`. Because it
      came from a long-lived user token, **it does not expire** — it stays
      valid until you change your password, revoke the app, or lose admin
      rights on the Page. Those are the three values for `.env`.

      This call also returns each Page's `instagram_business_account`, which
      is the `instagram_user_id` the config wants.

   d. **Keep the long-lived user token from step (b) as well.** Page tokens
      cover Page content, but they cannot read an ad account: `ads_read` is
      granted to the *user*, not the Page, so `/act_<id>/ads` with a Page
      token returns `(#100) Unsupported get request`. That token goes in
      `FB_ADS_USER_TOKEN` and is what reaches your ad comments.

      It expires after roughly 60 days, unlike the Page tokens. When ad
      comments begin reporting as "not checked", that is why — repeat step
      (b) and paste the new value. To avoid the renewal entirely, create a
      System User in Business settings and use its token, which does not
      expire.

   **Never run with `--verbose` and share the output.** urllib3 logs each
   request URL at debug level and Graph carries the token in the query
   string. The CLI now silences that logger for exactly this reason, but
   any tool that logs a Graph URL will leak a live credential.

   Treat these tokens like passwords. Anyone holding one can read
   everything the permissions allow. Keep them in `.env`, never in git, and
   never paste them into a chat window or an issue.

   **Graph hides a token in its own responses.** Any paged reply carries a
   `paging.next` URL with the live `access_token` embedded in the query
   string:

   ```json
   "paging": { "next": "https://graph.facebook.com/v26.0/...&access_token=EAAZB8..." }
   ```

   So a response that looks like harmless IDs is not safe to share. Strip
   the whole `paging` block before pasting Graph output anywhere. (This is
   also why `GraphClient` rebuilds the query from that cursor rather than
   passing the URL through, and why its errors report a path with the query
   string removed.)

   To revoke a token that has leaked, send `DELETE /me/permissions` for the
   app. That invalidates every token issued to it; you then re-grant to get
   a fresh one.

### 3. Find your IDs

Step 4c above already returns each Page's ID and its linked Instagram
account. Ad account IDs are in the table further down, or:

```bash
curl -s "https://graph.facebook.com/v26.0/me/adaccounts\
?fields=id,name&access_token=LONG_LIVED_USER_TOKEN"
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

#### On an always-on Linux box (the NUC)

```bash
git clone <this repo> && cd facebook-monitor
cp accounts.example.yaml accounts.yaml   # fill in
cp .env.example .env                     # add the Page tokens
sudo ./deploy/install.sh
```

That creates a virtualenv, installs dependencies, locks `.env` to 0600, and
enables a systemd timer that runs every 15 minutes. It is safe to re-run to
pick up changes.

```bash
systemctl list-timers fbmonitor.timer          # when it next runs
sudo systemctl start fbmonitor.service         # run it right now
journalctl -u fbmonitor.service -n 50          # history of runs
cat digest.txt                                 # the digests themselves
```

The timer uses `Persistent=true`, so if the box is off or asleep at a
scheduled tick it runs as soon as it is back rather than skipping that
window. The service is sandboxed (`ProtectSystem=strict`, `ProtectHome`,
`NoNewPrivileges`) because it only ever reads a remote API and writes two
files of its own.

#### Plain cron, or a non-systemd box

```cron
*/15 * * * * cd /path/to/facebook-monitor && \
  set -a && . ./.env && set +a && \
  ./.venv/bin/python -m fbmonitor >> digest.txt 2>&1
```

#### On Windows (the NUC)

Open PowerShell **as Administrator** in the checkout, then:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\deploy\install.ps1
```

That creates a virtualenv, installs dependencies, restricts `.env` to your
user with `icacls`, and registers a Scheduled Task running every 15 minutes.
Safe to re-run.

```powershell
Start-ScheduledTask -TaskName FacebookMonitor          # run it now
Get-ScheduledTask -TaskName FacebookMonitor | Get-ScheduledTaskInfo
Get-Content .\digest.txt -Tail 40                      # recent digests
.\deploy\run.ps1 -Preview                              # look without consuming
```

The task is set `-StartWhenAvailable`, so a run missed while the machine was
off happens on return rather than being skipped.

### Getting the digest to a human

A digest on a machine nobody logs into is not a monitor. With `--notify`
(which both installers pass), each run posts to the Slack, Discord or
Telegram webhook in `$FBMONITOR_WEBHOOK_URL`.

It is deliberately quiet: **a run with nothing new posts nothing at all.** A
channel that pings every fifteen minutes with "no change" gets muted within
a day, and a muted channel is worse than no channel. It speaks when there
are new items, or when a source broke — a dead token must not read as a
quiet day. A source that is merely unconfigured never triggers a post,
since it would report identically forever.

Complaints lead the message, and it is capped to fit Discord's 2000-character
limit; `digest.txt` always holds the full detail.

#### How often

Every 15 minutes is the gap between a hostile comment appearing under a
live ad and you seeing it, and it sits well inside the rate limits for a
couple of Pages. Trim it if that is too slow. Below roughly five minutes,
a Page webhook subscription is the better instrument — but that needs the
app published and a public HTTPS endpoint, which is a different piece of
work than a timer.

## Notes

- **Graph API version.** Defaults to `v26.0` (`fbmonitor/graph.py`), which
  is what the Graph API Explorer was serving when this was set up. Meta
  retires versions roughly two years after release, so check the
  [changelog](https://developers.facebook.com/docs/graph-api/changelog) and
  bump it — either edit the constant or pass `--api-version`.
- **Ad comments need the right token.** Comments on an ad live on the Page
  post behind it, so the token must be a Page token for the Page the ads run
  under. If it is not, the collector says so rather than reporting nothing.
- **Polling is not instant.** A 15-minute cron means a bad comment can sit
  under a live ad for up to 15 minutes. If that is too slow, the next step is
  a Page webhook subscription on the `feed` topic, which pushes comment
  events within seconds — it needs a public HTTPS endpoint to receive them,
  so it is real infrastructure rather than a cron line.
- **Recommendations have no stable ID** on every entry, so one is derived
  from the reviewer and timestamp to keep deduplication working.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

42 tests, no network — Graph is faked at the client seam.

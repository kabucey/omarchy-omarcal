# Orchard

A CalDAV calendar for the Omarchy shell: a clock in the bar, and a month,
week and day view behind it.

It talks to the server directly rather than going through Evolution Data
Server, so every request carries a real socket timeout and one slow calendar
degrades to an error on that calendar alone instead of hanging the widget.
Parsing, recurrence expansion and VTIMEZONE handling come from `libical`,
which is already on the system.

The helper remains Python for CalDAV, writes and calendar parsing. The
notification poll can use an optional Rust executable to read a SQLite
projection of upcoming alerts; the launcher falls back to Python when that
executable is not installed. The projection is refreshed when cached calendar
data changes and covers a rolling 32 days before and after the current time.

It supports iCloud with an app-specific password and Google Calendar through
OAuth in the system browser. Calendars that are only a URL — public iCal
feeds — are added by their address and fetched as-is: no sign-in, and
read-only, so nothing but their publisher can change them. Provider
credentials stay in the desktop keyring.

## What it does

- **Month, week and day views.** The week is drawn on its side — days down,
  hours across — so overlapping appointments stack and keep their names
  instead of splitting a column into stripes.
- **An event viewer**: when, how it repeats, where, its alerts, organiser,
  invitees, notes and any link it carries.
- **Desktop notifications** for the alerts attached to synced iCloud and
  Google events. They use Omarchy's notification history and respect Do Not
  Disturb; delivered alerts are remembered across shell restarts.
- **Search** across every synced calendar, grouped by day, newest first.
- **Several accounts**, each with its own calendars, shown or hidden
  individually and syncable one at a time — including a calendar that is
  nothing more than a public iCal URL.
- **Create, edit, duplicate and delete events** — title, calendar, all
  day, start and end each with its own time zone, repeat (presets, or a
  custom rule: every N days, weeks, months or years, chosen weekdays, "the
  fourth Tuesday", an end date or a count), address, video call, invitees,
  two alerts, travel time, notes, attachments and a link.
- **Repeating events** ask how far a change reaches — this event, this and
  following, or all — and are written the way calendar servers expect.
- **Nothing is overwritten.** Every save is conditional on the version the
  form opened: if the event changed on another device meanwhile, nothing is
  saved and the form says so.
- **Works offline.** A save that cannot reach the server is kept on this
  computer, shown at once as not sent yet, and sent with the next sync.
- **Settings** for the week start, 12- or 24-hour time, week numbers, the
  sync interval, the hours the day and week rails draw, the calendar new
  events go to, contact and address suggestions, and the bar clock's own
  face.

## Install

```bash
omarchy plugin add https://github.com/kabucey/omarchy-orchard.git --enable
```

For local development, link the checkout into the plugins directory instead,
so edits apply without reinstalling:

```bash
# from the root of your checkout
ln -s "$PWD" ~/.config/omarchy/plugins/com.buceylabs.orchard
omarchy-shell shell rescanPlugins
```

Then add the widget to the bar and open it: the calendar list on the left has
an **Add calendar** button at the bottom.

## Connecting to iCloud

Apple requires an app-specific password to give a third party access to your
calendar; your real Apple ID password will not work and is never asked for.
The form links to the steps, and generates nothing itself.

The password is stored in your keyring under the schema `com.buceylabs.orchard`
and is passed to the helper over stdin, never on a command line — `argv` is
readable from `/proc` by anything running as your user. Revoking the
app-specific password from Apple's account page removes this plugin's access
and nothing else's.

## Connecting to Google Calendar

Orchard uses Google's installed-app OAuth flow. Create a Desktop OAuth client
in your own Google Cloud project; credentials are not bundled with Orchard.

1. In the [Google Cloud console](https://console.cloud.google.com/), create or
   select a project. Enable both the
   [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
   and the [CalDAV API](https://console.cloud.google.com/apis/library/caldav.googleapis.com)
   in that same project. Orchard uses the Calendar API to discover calendars
   and CalDAV to sync their events.
2. Open **Google Auth Platform → Branding**. Set an app name, user support
   email, and contact email. Under **Audience**, choose **Internal** if this is
   only for your Google Workspace organization, or **External** for a personal
   Google account. If External is in **Testing**, add your Google account under
   **Test users**. For an External app, open **Data Access** and add the
   Calendar scope `https://www.googleapis.com/auth/calendar`.
3. Open **Google Auth Platform → Clients → Create client**, choose **Desktop
   app**, create it, and download the JSON file. This is the installed-app
   client type used by Orchard's loopback sign-in.
4. In Orchard, choose **Add calendar → Google → Import credentials**, select
   the downloaded JSON file, then choose **Continue with Google**. Sign in and
   grant access in your browser.

Google's [Calendar quickstart](https://developers.google.com/workspace/calendar/api/quickstart/python),
[OAuth consent setup guide](https://developers.google.com/workspace/guides/configure-oauth-consent),
[OAuth client setup guide](https://developers.google.com/workspace/guides/create-credentials),
and [installed-app OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app)
show the corresponding Cloud Console and desktop flow steps. Orchard requests
the `openid` and `email` identity scopes along with the Calendar scope. While
an External app remains in **Testing**, Google may expire its refresh tokens
after seven days because this app requests Calendar access; see Google's
[OAuth token expiration rules](https://developers.google.com/identity/protocols/oauth2#refresh-token-expiration).

Sign-in opens in your normal browser and returns through a short-lived
`127.0.0.1` callback protected by PKCE and a random state value. Orchard stores
only the refresh token in the keyring; access tokens stay in memory.

Google calendars can sync, and existing events can be edited or deleted with
`If-Match` conflict protection. Creating Google events, managed attachments,
and “this and following” series splits are intentionally unavailable for now:
Google's CalDAV API does not support the conditional-create header Orchard
uses to guarantee that a new resource cannot overwrite one already there.

## What leaves your computer

| Goes to | When | What |
| --- | --- | --- |
| Your calendar server (iCloud or Google) | every sync, and every supported save | your calendars and events |
| Google OAuth and Calendar APIs | when connecting Google and refreshing access | browser authorization, account identity, calendar names, colors, and access roles |
| iCloud Contacts | only if you allow it in Settings → Contacts | reads names and email addresses to suggest invitees |
| Photon (komoot) or Nominatim (OpenStreetMap) | only if you choose one in Settings → Address search | the address you are typing |
| GitHub | once a day, unless turned off in Settings → Updates | a request for orchard's latest release |

Contacts and address lookups are off until you choose them, and each asks
in so many words before anything is read or sent. Turning contacts off
deletes the ones kept. Address suggestions from your own calendar never
leave the machine. Invitations are sent by iCloud itself when an event
with invitees is saved.

Every request has a timeout, and the update check's answer is capped in
size before it is read.

## Requirements

Everything here ships with Omarchy; none of it comes from pip.

| Needs | For |
| --- | --- |
| `python3` | the CalDAV helper |
| `python-gobject` | `gi`, the binding layer |
| `libical-glib` | `ICalGLib 4.0` — parsing, recurrence, VTIMEZONE |
| `libsecret` | `secret-tool`, for the keyring |
| `wl-clipboard` | `wl-copy`, for the copy buttons |
| `xdg-desktop-portal-gtk` | the file dialog for attachments |
| `cargo` (optional) | build the Rust alert-poll executable |

## Where things are kept

| Path | Holds |
| --- | --- |
| `~/.local/state/orchard/cache.db` | synced events, calendars, accounts, settings, changes waiting to be sent, and — only if you allow it — contact names and addresses |
| `~/.config/orchard/google-client.json` | your private Google Desktop OAuth client configuration |
| keyring, schema `com.buceylabs.orchard` | iCloud app-specific passwords and Google refresh tokens |

An install from before the rename to Orchard keeps its data under
`omarcal`: the cache and Google client directories are moved across the first
time Orchard looks for them, and each keyring entry when it is first read.

Removing the plugin leaves both; `Disconnect account` in the account form
deletes an account's calendars, cached events and stored credential. For
Google, it also asks Google to revoke the grant and reports if remote
revocation could not be confirmed.

## Remove

```bash
omarchy plugin remove com.buceylabs.orchard
```

## Development

```bash
./test/all          # every suite, headless, no display needed
./helper/build-native # optional: build the faster alert-poll executable
```

`Logic.js` holds every layout and formatting decision the views make and is
covered by the suite; the QML only draws. The test fixture is a real month's
worth of shapes with the names, addresses and identifiers replaced.

The full test runner builds the native helper when Cargo is available. Without
Cargo or a built helper, it exercises the Python fallback. To compare repeated
poll latency on a private synthetic cache (no account data or network access),
build the native helper and run:

```bash
python3 bench/alerts.py --objects 500 --iterations 12
```

The benchmark checks that both backends return the same alerts and reports
their median poll time. Its temporary cache is removed when it exits.

## Credits

Orchard began as a fork of [omarcal](https://github.com/lancefaul/omarchy-omarcal)
by lancefaul, and is used and redistributed under its MIT License. The
original copyright notice is kept in [LICENSE](LICENSE), and the credit also
appears in the app under Settings → About.

## Licence

MIT — see [LICENSE](LICENSE).

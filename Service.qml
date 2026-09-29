import QtQuick
import Quickshell
import Quickshell.Io
import "Logic.js" as Logic

// Everything that talks to the omarcal helper launcher.
//
// The helper always prints one JSON object and exits 0, so a failure arrives
// as `ok: false` with a code rather than as a crash or empty output. Nothing
// here blocks: a sync that is slow or wedged leaves the last cached events on
// screen and sets `error`, which is the whole reason omarcal does not go
// through Evolution Data Server.
QtObject {
  id: root

  property string account: ""
  property string accountId: ""
  property var accountDetails: null
  // Changes saved while offline and not yet on their account, or refused
  // when they were sent — as `status` last reported them.
  property int pendingCount: 0
  property var pendingProblems: []

  function dismissPending(id) {
    if (dismissProc.running) return
    dismissProc.command = [helper, "pending-dismiss", "--id", String(id)]
    dismissProc.running = true
  }

  property Process dismissProc: Process {
    running: false
    stdout: StdioCollector { id: dismissOut; waitForEnd: true }
    onExited: root.refreshStatus()
  }

  // The machine's zone, by IANA name, as the helper reads it.
  property string localZone: ""

  // Every zone, with today's offset, for the form's zone pickers. Asked for
  // once, the first time a form wants it.
  property var zones: []

  function loadZones() {
    if (zones.length || zonesProc.running) return
    zonesProc.command = [helper, "zones"]
    zonesProc.running = true
  }

  property Process zonesProc: Process {
    running: false
    stdout: StdioCollector { id: zonesOut; waitForEnd: true }
    onExited: {
      var payload = root.parse(zonesOut.text, "the time zones did not come back")
      if (payload && payload.ok) {
        root.zones = payload.zones || []
        if (payload.local) root.localZone = payload.local
      }
    }
  }
  property string server: ""
  property var accounts: []
  property var events: []
  property var calendars: []
  property bool loading: false
  // Panel preferences, as `status` last reported them. Empty until the first
  // status lands, which is what settingsLoaded says.
  property var settings: ({})
  property bool settingsLoaded: false
  property double cacheBytes: 0
  property int objectCount: 0
  property bool syncing: false
  property string error: ""
  property double lastSync: 0

  // The window currently loaded, as YYYY-MM-DD. The panel widens this as the
  // viewed month changes.
  property string rangeStart: ""
  property string rangeEnd: ""

  readonly property string helper: decodeURIComponent(
    Qt.resolvedUrl("helper/omarcal").toString().replace(/^file:\/\//, ""))

  signal eventsLoaded()
  signal accountAdded(int requestId)
  signal googleClientImportFinished(bool imported)

  property bool addingAccount: false
  // The Google sign-in runs here, rather than in the panel, so hiding the
  // card does not lose the browser round trip. The request id lets a reopened
  // form attach to this attempt and rejects stale completions.
  property string accountAuthProvider: ""
  property string accountAuthState: "idle"
  property int accountAuthRequestId: -1
  property bool importingGoogleClient: false
  property string googleClientImportError: ""
  property string googleClientImportNotice: ""
  property string addError: ""
  property int addErrorRequestId: -1
  property string removeError: ""
  property string notice: ""
  property bool disconnectingAccount: false
  // Only held while the panel is showing it, and dropped the moment it stops.
  property string revealedPassword: ""

  function parse(text, fallback) {
    try {
      var payload = JSON.parse(String(text || "").trim())
      return payload && typeof payload === "object" ? payload : null
    } catch (e) {
      root.error = fallback
      return null
    }
  }

  // Brief, non-blocking account notices survive the status/sync refresh that
  // follows login or disconnect. They are distinct from actionable failures.
  function showNotice(message) {
    notice = String(message || "")
    if (notice) noticeTimeout.restart()
    else noticeTimeout.stop()
  }

  property Timer noticeTimeout: Timer {
    interval: 30000
    onTriggered: root.notice = ""
  }

  // Load occurrences for a window. Cheap — it reads the local cache and
  // expands only the objects that can fall inside it.
  function load(from, to) {
    if (!from || !to) return
    if (from === rangeStart && to === rangeEnd && events.length > 0) return
    rangeStart = from
    rangeEnd = to
    if (eventsProc.running) eventsProc.running = false
    eventsProc.command = [helper, "events", "--from", from, "--to", to]
    loading = true
    eventsProc.running = true
  }

  function reload() {
    var from = rangeStart, to = rangeEnd
    rangeStart = ""
    rangeEnd = ""
    load(from, to)
  }

  // ------------------------------------------------------------- prewarm
  //
  // The panel opens on the current month, and it is built before anyone
  // reaches for it — but its first load still lands on a click if it did
  // not already, and there is no loading state, so that first open flashes
  // empty and then fills. The service knows the month it will open on, so
  // it loads that same window on its own, a moment after `status` hands it
  // the calendars, so a click finds the months already in memory.
  //
  // It warms only while nothing is loaded: a real load owns `events` from
  // then on, and a background prewarm must never clobber a window the panel
  // is showing. `load()` skips a window it already has, so an open on that
  // same month is a no-op rather than a second spawn.
  function prewarm() {
    if (!settingsLoaded || eventsProc.running) return
    if (rangeStart && rangeEnd && events.length) return
    if (!helper) return
    var now = new Date()
    var grid = Logic.monthGrid(
      now.getFullYear(), now.getMonth() + 1,
      Number(setting("weekStartDay", 0)) || 0, "")
    if (!grid.length) return
    load(Logic.addDays(grid[0].key, -1),
         Logic.addDays(grid[grid.length - 1].key, 2))
  }

  // Pull from the server, then reload the window. An unchanged ctag makes
  // this nearly free, so it is safe to call on a timer.
  property bool syncQueued: false

  function sync() {
    if (syncProc.running) {
      syncQueued = true
      return
    }
    syncQueued = false
    syncProc.command = [helper, "sync"]
    syncing = true
    syncProc.running = true
  }

  // One calendar, for when only that one is suspected of being behind.
  property string syncingCalendar: ""

  function syncCalendar(url) {
    if (syncProc.running) return
    syncingCalendar = url
    syncProc.command = [helper, "sync", "--calendar", url]
    syncing = true
    syncProc.running = true
  }

  // Everything an account brought, and its saved sign-in with it.
  function removeAccount(accountId) {
    if (removeProc.running) return
    disconnectingAccount = true
    removeError = ""
    showNotice("")
    removeProc.command = [helper, "remove-account", "--account-id", accountId]
    removeProc.running = true
  }

  // Stops syncing one calendar and lets go of what was cached for it.
  function forgetCalendar(url) {
    if (removeProc.running) return
    disconnectingAccount = false
    removeProc.command = [helper, "forget-calendar", "--calendar", url]
    removeProc.running = true
  }

  signal removed()

  property Process removeProc: Process {
    running: false
    stdout: StdioCollector { id: removeOut; waitForEnd: true }
    onExited: {
      var payload = root.parse(removeOut.text, "the removal did not report back")
      if (root.disconnectingAccount) {
        if (!payload) root.removeError = root.error || "the removal did not report back"
        else if (!payload.ok) root.removeError = payload.error || "could not remove it"
        else {
          // Local removal is complete even when Google could not confirm a
          // remote token revocation. Keep that informational notice visible
          // even while the event list and status are refreshed.
          root.showNotice(Logic.accountResponseNotice(payload))
          root.removeError = ""
          root.removed()
        }
      } else if (payload && !payload.ok) {
        // Forgetting a calendar shares this process, but still reports its
        // failure through the service's general error channel.
        root.error = payload.error || "could not remove it"
      }
      root.disconnectingAccount = false
      root.refreshStatus()
      root.reload()
    }
  }

  // --------------------------------------------------------------- updates
  //
  // The installed version is read from the manifest at runtime rather than
  // written into QML, so it cannot drift from what was released. Everything
  // the check remembers goes in the helper's settings table with the rest —
  // an update reloads the plugin and takes this object with it, so what was
  // in flight has to be written down before it runs.

  property string installedVersion: ""
  property var latestRelease: null
  property bool updateChecking: false
  property string updateError: ""
  property bool updateRunning: false

  readonly property double updateCheckedAt:
    Number(setting("updateCheckedAt", 0)) || 0
  readonly property bool updateCheck: setting("updateCheck", true) !== false
  readonly property string updateDismissed: String(setting("updateDismissed", ""))
  readonly property bool updateAvailable:
    latestRelease !== null && installedVersion !== ""
    && Logic.isNewerVersion(latestRelease.version, installedVersion)

  property FileView manifestFile: FileView {
    path: Qt.resolvedUrl("manifest.json").toString().replace(/^file:\/\//, "")
    printErrors: false
    onLoaded: {
      try {
        var parsed = JSON.parse(text() || "{}")
        root.installedVersion = String(parsed && parsed.version || "")
      } catch (e) {
        root.installedVersion = ""
      }
      root.reportUpdateAttempt()
    }
  }

  // What became of an update started before the plugin reloaded. Read once,
  // then cleared: it is a note to whatever loads next, not a setting.
  signal updateFinished(string outcome)

  function reportUpdateAttempt() {
    var raw = String(setting("updateAttempt", ""))
    if (!raw) return
    var attempt = null
    try { attempt = JSON.parse(raw) } catch (e) { attempt = null }
    var outcome = Logic.updateAttemptState(attempt, installedVersion, Date.now())
    if (!outcome) return
    setSetting("updateAttempt", "")
    updateFinished(outcome)
  }

  // Asked through the helper, which bounds the request: a timeout on every
  // read, a deadline on the whole, and a cap on the answer's size before any
  // of it is buffered. The shell is long-lived; an unbounded request here
  // could hang the check or fill its memory. The timer below is the shell's
  // own backstop, in case the helper itself does not come back.
  function checkForUpdate() {
    if (updateChecking) return
    updateChecking = true
    updateError = ""
    updateProc.command = [helper, "update-check"]
    updateProc.running = true
    updateDeadline.restart()
  }

  property Process updateProc: Process {
    running: false
    stdout: StdioCollector { id: updateOut; waitForEnd: true }
    onExited: {
      updateDeadline.stop()
      if (!root.updateChecking) return
      root.updateChecking = false
      var payload = root.parse(updateOut.text, "")
      if (!payload || !payload.ok) {
        root.updateError = payload && payload.error ? payload.error : "GitHub could not be reached"
      } else if (payload.status === 404) {
        // No release yet is not a failure; it is an answer.
        root.latestRelease = null
      } else {
        var release = Logic.parseLatestRelease(payload.body)
        if (release) root.latestRelease = release
        else root.updateError = "GitHub's answer could not be read"
      }
      root.setSetting("updateCheckedAt", Date.now())
    }
  }

  property Timer updateDeadline: Timer {
    interval: 25000
    repeat: false
    onTriggered: {
      if (!root.updateChecking) return
      root.updateChecking = false
      updateProc.running = false
      root.updateError = "GitHub took too long to answer"
      root.setSetting("updateCheckedAt", Date.now())
    }
  }

  // The attempt is written before the command runs, because the command is
  // what stops this object existing.
  function applyUpdate() {
    if (updateRunning || !latestRelease) return
    updateRunning = true
    setSetting("updateAttempt", JSON.stringify(
      { version: latestRelease.version, at: Date.now() }))
    Quickshell.execDetached(Logic.updateCommand())
  }

  function dismissUpdate() {
    if (latestRelease) setSetting("updateDismissed", latestRelease.version)
  }

  property Timer updateTimer: Timer {
    interval: 60 * 60 * 1000
    repeat: true
    running: true
    onTriggered: if (Logic.updateCheckDue(root.updateCheckedAt, Date.now(),
                                          root.updateCheck))
      root.checkForUpdate()
  }

  // ---------------------------------------------------------------- search
  //
  // Runs over the cache, not the server: every calendar that has been synced
  // is already here, so a search is a LIKE over a local table and answers in
  // milliseconds. The panel debounces; this just runs what it is given.

  property var searchResults: []
  property bool searching: false
  property string searchQuery: ""

  function search(query) {
    var text = String(query || "").trim()
    searchQuery = text
    if (!text) { clearSearch(); return }
    searching = true
    searchProc.running = false
    searchProc.command = [helper, "search", "--query", text,
                          "--limit", String(Logic.searchLimit())]
    searchProc.running = true
  }

  function clearSearch() {
    searchProc.running = false
    searchResults = []
    searching = false
  }

  property Process searchProc: Process {
    running: false
    stdout: StdioCollector { id: searchOut; waitForEnd: true }
    onExited: {
      root.searching = false
      var payload = root.parse(searchOut.text, "the search did not come back")
      root.searchResults = payload && payload.ok ? (payload.events || []) : []
    }
  }

  // ------------------------------------------------------------ one event
  //
  // The viewer asks for the event by its calendar object URL. UIDs can be
  // reused by separate calendars; a recurring one also needs the occurrence
  // `rid`, or the helper answers with the master and shows the wrong day.

  property var eventDetail: null
  property bool eventLoading: false
  property string eventError: ""

  // `at` is the open occurrence's start, so the helper can say what its
  // times read in the event's own zones for that occurrence, not the first.
  function loadEvent(uid, rid, at, href) {
    if (!uid) return
    // The last answer goes immediately: a viewer that opens showing the
    // previous event while this one loads is worse than one that opens empty.
    eventDetail = null
    eventError = ""
    eventLoading = true
    detailProc.running = false
    var command = [helper, "event", "--uid", uid]
    if (href) command = command.concat(["--href", href])
    if (rid) command = command.concat(["--rid", rid])
    if (at) command = command.concat(["--at", at])
    detailProc.command = command
    detailProc.running = true
  }

  function clearEvent() {
    detailProc.running = false
    eventDetail = null
    eventError = ""
    eventLoading = false
  }

  property Process detailProc: Process {
    running: false
    stdout: StdioCollector { id: detailOut; waitForEnd: true }
    onExited: {
      root.eventLoading = false
      var payload = root.parse(detailOut.text, "the event did not come back")
      if (!payload) { root.eventError = root.error; return }
      if (!payload.ok) {
        root.eventError = payload.error || "could not read that event"
        return
      }
      root.eventDetail = payload.event || null
    }
  }

  property bool statusRefreshQueued: false
  property int settingsRevision: 0

  function refreshStatus() {
    if (statusProc.running) {
      statusRefreshQueued = true
      return
    }
    statusRefreshQueued = false
    statusProc.settingsRevision = settingsRevision
    statusProc.startedWithPendingSettings =
      settingsQueue.length > 0 || settingProc.running
    statusProc.command = [helper, "status"]
    statusProc.running = true
  }

  // Hand the stored password back so the panel can show it. Its own command
  // rather than part of `status`, so a secret is only ever read on purpose.
  function revealPassword(user) {
    if (revealProc.running) return
    revealProc.command = [helper, "reveal-password", "--user", user]
    revealProc.running = true
  }

  function hidePassword() {
    revealedPassword = ""
  }

  property Process revealProc: Process {
    running: false
    stdout: StdioCollector { id: revealOut; waitForEnd: true }
    onExited: {
      var payload = root.parse(revealOut.text, "")
      root.revealedPassword = payload && payload.ok ? (payload.password || "") : ""
    }
  }

  // Add an account: discover its calendars and store the password. The
  // password goes over stdin, never argv — argv is readable from /proc by
  // anything running as this user, which is how the network panel does it too.
  function addAccount(user, server, password, requestId) {
    if (addingAccount || loginProc.running || googleLoginProc.running) return
    addError = ""
    addErrorRequestId = requestId
    addingAccount = true
    accountAuthProvider = "iCloud"
    accountAuthState = "connecting"
    accountAuthRequestId = requestId
    loginProc.requestId = requestId
    loginProc.timedOut = false
    loginProc.cancelled = false
    loginProc.secret = password
    loginProc.command = [helper, "login", "--user", user, "--server", server]
    loginProc.running = true
  }

  // A feed added from its URL: no credential to hand over, the helper
  // proves the address serves a calendar and then keeps it on its own.
  // Shares the login process with the password path — the helper reads the
  // secret off stdin only when it is a password account — and the same
  // request-id state, so a failed add lands on the form that asked for it.
  function addWebcalAccount(url, requestId) {
    if (addingAccount || loginProc.running || googleLoginProc.running) return
    addError = ""
    addErrorRequestId = requestId
    addingAccount = true
    accountAuthProvider = "webcal"
    accountAuthState = "connecting"
    accountAuthRequestId = requestId
    loginProc.requestId = requestId
    loginProc.timedOut = false
    loginProc.cancelled = false
    loginProc.secret = ""
    loginProc.command = [helper, "login", "--provider", "webcal", "--user", url]
    loginProc.running = true
  }

  function addGoogleAccount(requestId) {
    if (addingAccount || importingGoogleClient || loginProc.running
        || googleLoginProc.running || googleClientImportProc.running) return false
    addError = ""
    addErrorRequestId = requestId
    showNotice("")
    addingAccount = true
    accountAuthProvider = "Google"
    accountAuthState = "waiting"
    accountAuthRequestId = requestId
    googleLoginProc.requestId = requestId
    googleLoginProc.timedOut = false
    googleLoginProc.cancelled = false
    googleLoginProc.completed = false
    googleLoginProc.command = [helper, "login", "--provider", "google"]
    googleLoginProc.running = true
    return true
  }

  function importGoogleClient() {
    if (addingAccount || importingGoogleClient || loginProc.running
        || googleLoginProc.running || googleClientImportProc.running) return false
    googleClientImportError = ""
    googleClientImportNotice = ""
    importingGoogleClient = true
    googleClientImportProc.command = [helper, "import-google-client"]
    googleClientImportProc.running = true
    return true
  }

  function failGoogleLogin(requestId, message) {
    if (googleLoginProc.cancelled || googleLoginProc.timedOut
        || requestId !== accountAuthRequestId)
      return
    googleLoginProc.completed = true
    root.googleLoginTimeout.stop()
    root.addingAccount = false
    root.accountAuthState = "error"
    root.accountAuthRequestId = -1
    root.addErrorRequestId = requestId
    var safeMessage = Logic.singleLine(message || "Google sign-in could not be started")
    root.addError = safeMessage.length > 300
      ? safeMessage.slice(0, 297) + "…" : safeMessage
  }

  function cancelGoogleAccount(requestId) {
    if (accountAuthProvider !== "Google"
        || accountAuthRequestId !== requestId)
      return false
    googleLoginTimeout.stop()
    googleLoginProc.cancelled = true
    googleLoginProc.completed = true
    // Invalidate before stopping the helper so its exit cannot win the race.
    accountAuthRequestId = -1
    accountAuthState = "idle"
    accountAuthProvider = ""
    addingAccount = false
    addError = ""
    addErrorRequestId = -1
    if (googleLoginProc.running) googleLoginProc.running = false
    return true
  }

  // Nothing the helper does should take this long; if it somehow does, the
  // form has to come back rather than sit on "Saving…" forever.
  property Timer loginTimeout: Timer {
    interval: 60 * 1000
    onTriggered: {
      if (!loginProc.running) return
      loginProc.timedOut = true
      loginProc.running = false
      root.addingAccount = false
      root.accountAuthState = "error"
      root.accountAuthRequestId = -1
      root.addErrorRequestId = loginProc.requestId
      root.addError = "the server did not answer in time"
    }
  }

  property Process loginProc: Process {
    property string secret: ""
    property int requestId: -1
    property bool timedOut: false
    property bool cancelled: false
    running: false
    stdinEnabled: true
    stdout: StdioCollector { id: loginOut; waitForEnd: true }
    onStarted: {
      timedOut = false
      write(secret + "\n")
      secret = ""
      root.loginTimeout.restart()
    }
    onExited: {
      root.loginTimeout.stop()
      root.addingAccount = false
      if (timedOut) {
        timedOut = false
        return
      }
      if (cancelled) {
        cancelled = false
        return
      }
      var payload = root.parse(loginOut.text, "the helper did not report back")
      if (!payload) {
        root.accountAuthState = "error"
        root.accountAuthRequestId = -1
        root.addErrorRequestId = requestId
        root.addError = root.error
        return
      }
      if (!payload.ok) {
        root.accountAuthState = "error"
        root.accountAuthRequestId = -1
        root.addErrorRequestId = requestId
        root.addError = payload.error || "could not add the account"
        return
      }
      root.addErrorRequestId = -1
      root.addError = ""
      root.accountAuthState = "success"
      root.accountAuthRequestId = -1
      root.revealedPassword = ""
      root.refreshStatus()
      root.sync()
      root.accountAdded(requestId)
    }
  }

  // The helper opens the system browser and waits for Google's loopback
  // callback. This process receives only the final JSON result; credentials
  // never pass through QML. Five minutes leaves room for account selection
  // and consent while still returning a lost sign-in to a retryable state.
  property Timer googleLoginTimeout: Timer {
    interval: 5 * 60 * 1000
    onTriggered: {
      if (!googleLoginProc.running) return
      googleLoginProc.timedOut = true
      googleLoginProc.completed = true
      googleLoginProc.running = false
      root.addingAccount = false
      root.accountAuthState = "error"
      root.accountAuthRequestId = -1
      root.addErrorRequestId = googleLoginProc.requestId
      root.addError = "Google sign-in timed out. Start again to reconnect."
    }
  }

  property Process googleLoginProc: Process {
    property int requestId: -1
    property bool timedOut: false
    property bool cancelled: false
    property bool completed: false
    running: false
    stdout: StdioCollector { id: googleLoginOut; waitForEnd: true }
    onStarted: {
      if (cancelled || completed) {
        running = false
        return
      }
      root.accountAuthState = "waiting"
      root.googleLoginTimeout.restart()
    }
    onExited: {
      root.googleLoginTimeout.stop()
      if (timedOut || cancelled || completed) {
        timedOut = false
        cancelled = false
        completed = false
        return
      }
      root.addingAccount = false
      // Keep an OAuth protocol failure on the account form, not in the
      // calendar's general sync-error channel.
      var payload = null
      try {
        payload = JSON.parse(String(googleLoginOut.text || "").trim())
      } catch (e) { }
      if (!payload || typeof payload !== "object") {
        root.failGoogleLogin(requestId, "Google did not report a sign-in result")
        return
      }
      if (!payload.ok) {
        root.failGoogleLogin(requestId,
          payload.error || "Google sign-in could not be completed")
        return
      }
      completed = true
      root.addErrorRequestId = -1
      root.addError = ""
      root.accountAuthState = "success"
      root.accountAuthRequestId = -1
      root.refreshStatus()
      root.sync()
      root.showNotice(Logic.accountResponseNotice(payload))
      root.accountAdded(requestId)
    }
  }

  // The helper owns the desktop file picker and copies a valid Desktop OAuth
  // client into omarcal's private config directory. Running it as a process
  // keeps the picker and file validation off the UI thread.
  property Process googleClientImportProc: Process {
    running: false
    stdout: StdioCollector { id: googleClientImportOut; waitForEnd: true }
    onExited: {
      root.importingGoogleClient = false
      var payload = null
      try {
        payload = JSON.parse(String(googleClientImportOut.text || "").trim())
      } catch (e) { }
      var result = Logic.googleClientImportResult(payload)
      root.googleClientImportError = result.state === "error" ? result.message : ""
      root.googleClientImportNotice = result.state === "imported" ? result.message : ""
      root.googleClientImportFinished(result.state === "imported")
    }
  }

  // Show or hide one calendar. The helper stores the flag, so the choice
  // survives a restart; the list is updated locally first so the checkbox
  // responds immediately rather than after a round trip.
  function setCalendarEnabled(url, enabled) {
    if (toggleProc.running) return
    var next = []
    for (var i = 0; i < calendars.length; i++) {
      var entry = calendars[i]
      next.push(entry.url === url ? Object.assign({}, entry, { enabled: enabled }) : entry)
    }
    calendars = next
    toggleProc.command = [helper, "set-calendar", "--calendar", url,
                          "--enabled", enabled ? "true" : "false"]
    toggleProc.running = true
  }

  // Several at once, one after another: the helper writes one calendar per
  // run, and setCalendarEnabled drops a call while another is in flight.
  property var pendingStates: []

  function applyCalendarStates(changes) {
    if (!changes || !changes.length) return
    var queued = []
    for (var i = 0; i < changes.length; i++) queued.push(changes[i])
    pendingStates = queued
    runNextState()
  }

  function runNextState() {
    if (toggleProc.running || !pendingStates.length) return
    var next = pendingStates[0]
    pendingStates = pendingStates.slice(1)
    // Keep the list in step as each one goes, so the switches do not snap
    // back between runs.
    var updated = []
    for (var i = 0; i < calendars.length; i++) {
      var entry = calendars[i]
      updated.push(entry.url === next.url
        ? Object.assign({}, entry, { enabled: next.enabled }) : entry)
    }
    calendars = updated
    toggleProc.command = [helper, "set-calendar", "--calendar", next.url,
                          "--enabled", next.enabled ? "true" : "false"]
    toggleProc.running = true
  }

  property Process toggleProc: Process {
    running: false
    stdout: StdioCollector { id: toggleOut; waitForEnd: true }
    onExited: {
      if (root.pendingStates.length) { root.runNextState(); return }
      root.reload()
      root.refreshStatus()
    }
  }

  // Recolour one calendar. The helper stores the choice, so it survives a
  // restart and a refetch; the list is updated locally first so the picker
  // swatch responds immediately rather than after a round trip.
  function setCalendarColor(url, color) {
    if (colorProc.running) return
    var next = []
    for (var i = 0; i < calendars.length; i++) {
      var entry = calendars[i]
      next.push(entry.url === url ? Object.assign({}, entry, { color: color }) : entry)
    }
    calendars = next
    colorProc.command = [helper, "set-calendar", "--calendar", url, "--color", color]
    colorProc.running = true
  }

  property Process colorProc: Process {
    running: false
    stdout: StdioCollector { id: colorOut; waitForEnd: true }
    onExited: {
      root.reload()
      root.refreshStatus()
    }
  }

  property Process eventsProc: Process {
    running: false
    stdout: StdioCollector { id: eventsOut; waitForEnd: true }
    onExited: {
      root.loading = false
      var payload = root.parse(eventsOut.text, "could not read the event list")
      if (!payload) return
      if (payload.ok) {
        root.error = ""
        root.events = payload.events || []
        root.eventsLoaded()
      } else {
        root.error = payload.error || "the calendar could not be read"
      }
    }
  }

  property Process syncProc: Process {
    running: false
    stdout: StdioCollector { id: syncOut; waitForEnd: true }
    onExited: {
      root.syncing = false
      root.syncingCalendar = ""
      if (root.syncQueued) {
        root.syncQueued = false
        Qt.callLater(function() { root.sync() })
      }
      var payload = root.parse(syncOut.text, "the sync did not report back")
      if (!payload) return
      if (!payload.ok) {
        root.error = payload.error || "the sync failed"
        return
      }
      // Per-calendar errors do not stop the others; surface the first.
      var failed = (payload.calendars || []).filter(function (c) { return c.ok === false })
      root.error = failed.length ? (failed[0].name + ": " + (failed[0].error || "failed")) : ""
      root.lastSync = Date.now()
      var moved = (payload.calendars || []).some(function (c) {
        return c.added || c.changed || c.removedCount
      })
      if (moved) { root.reload(); root.loadPlacesHistory() }
      root.refreshStatus()
      // An address book changes slowly and has no sync token here, so it is
      // fetched whole — once a day is plenty.
      if (root.contactsEnabled && Date.now() - root.contactsSyncedAt > 24 * 60 * 60 * 1000)
        root.syncContacts()
    }
  }

  property Process statusProc: Process {
    property int settingsRevision: 0
    property bool startedWithPendingSettings: false
    running: false
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    onExited: {
      var payload = root.parse(statusOut.text, "")
      if (payload && payload.ok) {
        root.accounts = payload.accounts || []
        var primary = payload.account || root.accounts[0] || null
        if (primary && root.accounts.length) {
          for (var i = 0; i < root.accounts.length; i++) {
            var row = root.accounts[i]
            if ((primary.id && row.id === primary.id)
                || (!primary.id && row.user === primary.user
                    && (!primary.provider || row.provider === primary.provider))) {
              primary = row
              break
            }
          }
        }
        root.accountDetails = primary
        if (!root.accounts.length && root.accountDetails)
          root.accounts = [root.accountDetails]
        var rawCalendars = payload.calendars || []
        root.calendars = rawCalendars.map(function(calendar) {
          var owner = String(calendar.accountId || calendar.account || "")
          var matched = null
          for (var i = 0; i < root.accounts.length; i++) {
            var candidate = root.accounts[i]
            if (String(candidate.id || candidate.user || "") === owner) {
              matched = candidate
              break
            }
          }
          // Older status payloads used the email as calendar.account. Accept
          // that only when it identifies exactly one account; same-address
          // Google and iCloud accounts must never borrow each other's scope.
          if (!matched) {
            var legacyMatches = root.accounts.filter(function(candidate) {
              return String(candidate.user || candidate.email || "") === owner
            })
            if (legacyMatches.length === 1) matched = legacyMatches[0]
          }
          var user = matched
            ? String(matched.user || matched.email || owner) : owner
          var accountId = matched
            ? String(matched.id || matched.user || owner) : owner
          var provider = matched ? Logic.accountProviderName(matched) : ""
          var label = user
          if (root.accounts.length > 1) {
            if (provider) label = provider + " · " + user
            else if (/^(google|icloud):/i.test(owner))
              label = owner.toLowerCase().indexOf("google:") === 0
                ? "Google account" : "iCloud account"
          }
          return Object.assign({}, calendar, {
            accountId: accountId,
            accountUser: user,
            accountProvider: provider,
            accountLabel: label
          })
        })
        root.account = root.accountDetails ? root.accountDetails.user : ""
        root.accountId = root.accountDetails
          ? (root.accountDetails.id || root.accountDetails.user || "") : ""
        root.localZone = payload.localZone || ""
        root.pendingCount = payload.pending || 0
        root.pendingProblems = payload.pendingProblems || []
        root.server = root.accountDetails ? root.accountDetails.server : ""
        root.settings = Logic.mergePendingSettings(
          payload.settings || ({}), root.settings,
          Logic.statusSettingsNeedOverlay(
            settingsRevision, root.settingsRevision,
            startedWithPendingSettings,
            root.settingsQueue.length > 0 || root.settingProc.running))
        root.cacheBytes = payload.cacheBytes || 0
        root.objectCount = payload.objects || 0
        root.settingsLoaded = true
        if (!root.alertCursor)
          Qt.callLater(function() { root.pollAlerts() })
        // `status` just handed back the calendars, so the first month can
        // warm now, in the background, long before anyone reaches for it.
        root.prewarm()
      }
      if (root.statusRefreshQueued) {
        root.statusRefreshQueued = false
        root.refreshStatus()
      }
    }
  }

  // --------------------------------------------------------------- settings
  //
  // The helper's `settings` table is the only copy: shell.json belongs to the
  // shell and a plugin cannot write it, so the manifest's `defaults` are a
  // starting point and everything after that lives here. `status` carries the
  // whole set, which is why reading them costs no extra call.

  function setting(name, fallback) {
    var value = settings[name]
    return value === undefined || value === null ? fallback : value
  }

  // Applied here first and then written, so a switch moves under the pointer
  // instead of after a round trip. The helper answers with the whole set,
  // which then replaces this optimistic copy.
  function setSetting(name, value) {
    var next = {}
    for (var key in settings) next[key] = settings[key]
    next[name] = value
    settings = next
    settingsRevision += 1
    settingsQueue.push(name + "=" + JSON.stringify(value))
    runNextSetting()
  }

  property var settingsQueue: []

  function runNextSetting() {
    if (settingProc.running || !settingsQueue.length) return
    var pair = settingsQueue.shift()
    settingProc.command = [helper, "settings", "--set", pair]
    settingProc.running = true
  }

  property Process settingProc: Process {
    running: false
    stdout: StdioCollector { id: settingOut; waitForEnd: true }
    onExited: {
      var payload = root.parse(settingOut.text, "the setting did not save")
      // Only once the queue is empty: an answer from the first of three
      // writes would otherwise undo the two still to go.
      if (payload && payload.ok && payload.settings && !root.settingsQueue.length)
        root.settings = payload.settings
      root.runNextSetting()
      // Contacts are read only after the yes has been written down, so the
      // helper — which refuses while the setting is off — sees it on.
      if (root.contactsSyncWanted && !root.settingsQueue.length && !root.settingProc.running) {
        root.contactsSyncWanted = false
        root.syncContacts()
      }
    }
  }

  // --------------------------------------------------------- notifications
  //
  // Providers store event reminders as VALARMs in the same CalDAV objects
  // the helper already caches. The panel is lazy, so the headless service
  // owns the poll and hands due occurrences to Omarchy's notification CLI.

  // Writable because the shell injects its canonical path into services.
  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  readonly property string notificationSender: omarchyPath
    ? omarchyPath + "/bin/omarchy-notification-send"
    : "omarchy-notification-send"
  property double alertCursor: 0
  property double alertWindowEnd: 0

  function pollAlerts() {
    if (!settingsLoaded || alertsProc.running) return
    var now = Date.now()
    // A short grace catches a shell reload or brief suspend without producing
    // a backlog of stale notifications after the machine wakes hours later.
    var from = alertCursor > 0
      ? Math.max(alertCursor, now - 5 * 60 * 1000)
      : now - 2 * 60 * 1000
    alertWindowEnd = now
    alertsProc.command = [helper, "alerts",
                          "--from", new Date(from).toISOString(),
                          "--to", new Date(now).toISOString()]
    alertsProc.running = true
  }

  function alertBody(alert) {
    var day = Logic.longDate(Logic.dateKey(alert.start || ""))
    var when = alert.allDay ? "All day"
      : Logic.formatTime(alert.start || "", String(setting("timeFormat", "12h")))
    var body = day ? day + (when ? " at " + when : "") : when
    if (alert.calendar) body += (body ? "\n" : "") + String(alert.calendar)
    var location = String(alert.location || "").replace(/\s+/g, " ").trim()
    if (location) body += (body ? " — " : "") + location
    return body
  }

  function deliverAlerts(alerts) {
    var stored = setting("notifiedAlerts", [])
    var prior = Array.isArray(stored) ? stored : []
    var seen = ({})
    for (var i = 0; i < prior.length; i++) seen[String(prior[i])] = true

    var fresh = []
    var next = prior.slice(Math.max(0, prior.length - 511))
    for (var a = 0; a < (alerts || []).length; a++) {
      var alert = alerts[a] || ({})
      var id = String(alert.id || "")
      if (!id || seen[id]) continue
      seen[id] = true
      next.push(id)
      fresh.push(alert)
    }
    if (!fresh.length) return

    // Optimistically update the service copy before starting notifications;
    // the normal settings queue persists the same IDs in SQLite.
    setSetting("notifiedAlerts", next.slice(Math.max(0, next.length - 512)))
    for (var n = 0; n < fresh.length; n++) {
      var item = fresh[n]
      Quickshell.execDetached([
        notificationSender,
        "--app-name", "lancefaul.omarcal",
        "-g", "󰃭",
        "-u", "critical",
        String(item.title || "Calendar event"),
        alertBody(item)
      ])
    }
  }

  property Process alertsProc: Process {
    running: false
    stdout: StdioCollector { id: alertsOut; waitForEnd: true }
    onExited: {
      var payload = root.parse(alertsOut.text, "")
      if (!payload || !payload.ok) return
      root.alertCursor = root.alertWindowEnd
      root.deliverAlerts(payload.alerts || [])
    }
  }

  property Timer alertsTimer: Timer {
    interval: 30 * 1000
    repeat: true
    running: true
    onTriggered: root.pollAlerts()
  }

  // -------------------------------------------------------------- writes
  //
  // Saving and deleting one event. The request goes to the helper as one
  // line of JSON on stdin — a Process cannot close its stdin, so the helper
  // reads a line rather than to the end — and the answer is handed to
  // whoever asked, then the window is reloaded so the change shows.

  property bool writing: false
  property string writeAction: ""
  property var writeRequest: null
  signal writeFinished(string action, var payload)

  function saveEvent(request) { startWrite("save", request) }
  function deleteEvent(request) { startWrite("delete", request) }

  function startWrite(action, request) {
    if (writeProc.running) return
    writing = true
    writeAction = action
    writeRequest = request
    writeProc.command = [helper, action]
    writeProc.running = true
  }

  property Process writeProc: Process {
    running: false
    stdinEnabled: true
    stdout: StdioCollector { id: writeOut; waitForEnd: true }
    onStarted: {
      write(JSON.stringify(root.writeRequest) + "\n")
      root.writeRequest = null
    }
    onExited: {
      var action = root.writeAction
      root.writing = false
      var payload = root.parse(writeOut.text, "the save did not report back")
      // A conflict means the cache is behind iCloud; a sync brings it level
      // so reopening the event shows what changed it.
      if (payload && !payload.ok && payload.code === "conflict") root.sync()
      if (payload && payload.ok) root.reload()
      root.refreshStatus()
      root.writeFinished(action, payload)
    }
  }

  // -------------------------------------------------------------- places
  //
  // The calendar's own addresses, always, read from the cache and never
  // sent anywhere; and a lookup service only once one has been chosen,
  // which the helper checks for itself before it sends a thing.

  readonly property string placesProvider: String(setting("placesProvider", "none"))
  property var placesHistory: []
  property var placeResults: []
  property string placeResultsFor: ""
  property bool placesSearching: false
  property string placesError: ""

  function loadPlacesHistory() {
    if (placesHistoryProc.running) return
    placesHistoryProc.command = [helper, "places-history"]
    placesHistoryProc.running = true
  }

  property Process placesHistoryProc: Process {
    running: false
    stdout: StdioCollector { id: placesHistoryOut; waitForEnd: true }
    onExited: {
      var payload = root.parse(placesHistoryOut.text, "addresses did not come back")
      if (payload && payload.ok) root.placesHistory = payload.places || []
    }
  }

  function searchPlaces(query) {
    var text = String(query || "").trim()
    if (placesProvider === "none" || text.length < 3) { clearPlaces(); return }
    placesProc.running = false
    placeResultsFor = text
    placesSearching = true
    placesError = ""
    placesProc.command = [helper, "places", "--query", text]
    placesProc.running = true
  }

  function clearPlaces() {
    placesProc.running = false
    placeResults = []
    placeResultsFor = ""
    placesSearching = false
    placesError = ""
  }

  function setPlacesProvider(provider) {
    clearPlaces()
    setSetting("placesProvider", provider)
  }

  property Process placesProc: Process {
    running: false
    stdout: StdioCollector { id: placesOut; waitForEnd: true }
    onExited: {
      root.placesSearching = false
      var payload = root.parse(placesOut.text, "the address search did not come back")
      if (!payload) return
      if (!payload.ok) { root.placesError = payload.error || "the address search failed"; root.placeResults = []; return }
      root.placeResults = payload.places || []
    }
  }

  // ------------------------------------------------------------ contacts
  //
  // Opt-in. Nothing here runs until the person has said yes in the panel's
  // own words; saying no again deletes what was kept. The helper enforces
  // both on its side too, so this is not the only thing standing between a
  // setting and an address book.

  readonly property bool contactsEnabled: setting("contactsEnabled", false) === true
  property var contacts: []
  property int contactCount: 0
  property string contactsError: ""
  property bool contactsSyncing: false
  property double contactsSyncedAt: 0
  property bool contactsSyncWanted: false

  function enableContacts() {
    contactsSyncWanted = true
    setSetting("contactsEnabled", true)
  }

  function disableContacts() {
    contactsSyncWanted = false
    contactsProc.running = false
    contacts = []
    contactCount = 0
    contactsError = ""
    setSetting("contactsEnabled", false)
  }

  function syncContacts() {
    if (!contactsEnabled || contactsProc.running) return
    contactsSyncing = true
    contactsProc.command = [helper, "contacts-sync"]
    contactsProc.running = true
  }

  // What is already kept, without going to iCloud for it.
  function loadContacts() {
    if (!contactsEnabled || contactsProc.running) return
    contactsProc.command = [helper, "contacts"]
    contactsProc.running = true
  }

  property Process contactsProc: Process {
    running: false
    stdout: StdioCollector { id: contactsOut; waitForEnd: true }
    onExited: {
      var syncing = root.contactsSyncing
      root.contactsSyncing = false
      var payload = root.parse(contactsOut.text, "contacts did not come back")
      if (!payload) return
      if (!payload.ok) { root.contactsError = payload.error || "contacts could not be read"; return }
      // Switched off while this was running: what came back is not wanted.
      if (!root.contactsEnabled) { root.contacts = []; root.contactCount = 0; return }
      root.contacts = payload.contacts || []
      root.contactCount = payload.count || 0
      root.contactsError = payload.error || ""
      if (syncing) root.contactsSyncedAt = Date.now()
    }
  }

  property Timer refreshTimer: Timer {
    // Honours refreshMinutes, and never faster than a minute: the interval is
    // a person's typed number, and a zero here would spin.
    interval: Math.max(1, Number(root.setting("refreshMinutes", 15)) || 15) * 60 * 1000
    repeat: true
    running: true
    onTriggered: root.sync()
  }

  // Nothing here runs while the shell is still loading its plugins.
  //
  // `omarchy plugin add` gives the shell two seconds to rescan, and spawning
  // the helper and reaching for GitHub inside that window is what put the
  // rescan over it. Settings come back almost at once so the bar clock has
  // its face; the sync and the update check are network work nobody is
  // waiting on, and they can start a beat later.
  property Timer settleTimer: Timer {
    interval: 400
    repeat: false
    running: true
    onTriggered: {
      root.refreshStatus()
      slowStartTimer.start()
    }
  }

  property Timer slowStartTimer: Timer {
    interval: 2600
    repeat: false
    onTriggered: {
      root.prewarm()
      root.sync()
      root.loadPlacesHistory()
      if (Logic.updateCheckDue(root.updateCheckedAt, Date.now(), root.updateCheck))
        root.checkForUpdate()
    }
  }
}

use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveDateTime};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::env;
use std::ffi::{CString, OsStr};
use std::os::raw::{c_char, c_int, c_uchar, c_void};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Instant;

const SQLITE_OPEN_READONLY: c_int = 0x0000_0001;
const SQLITE_OK: c_int = 0;
const SQLITE_ROW: c_int = 100;
const SQLITE_DONE: c_int = 101;
const SQLITE_INTEGER: c_int = 1;
const SQLITE_TEXT: c_int = 3;
const ALERT_EVENT_HORIZON_SECONDS: i64 = 32 * 24 * 60 * 60;

#[repr(C)]
struct Sqlite3 {
    _private: [u8; 0],
}

#[repr(C)]
struct SqliteStmt {
    _private: [u8; 0],
}

#[link(name = "sqlite3")]
extern "C" {
    fn sqlite3_open_v2(
        filename: *const c_char,
        db: *mut *mut Sqlite3,
        flags: c_int,
        vfs: *const c_char,
    ) -> c_int;
    fn sqlite3_close_v2(db: *mut Sqlite3) -> c_int;
    fn sqlite3_busy_timeout(db: *mut Sqlite3, milliseconds: c_int) -> c_int;
    fn sqlite3_exec(
        db: *mut Sqlite3,
        sql: *const c_char,
        callback: Option<
            unsafe extern "C" fn(*mut c_void, c_int, *mut *mut c_char, *mut *mut c_char) -> c_int,
        >,
        callback_arg: *mut c_void,
        error_message: *mut *mut c_char,
    ) -> c_int;
    fn sqlite3_prepare_v2(
        db: *mut Sqlite3,
        sql: *const c_char,
        bytes: c_int,
        statement: *mut *mut SqliteStmt,
        tail: *mut *const c_char,
    ) -> c_int;
    fn sqlite3_bind_int64(statement: *mut SqliteStmt, index: c_int, value: i64) -> c_int;
    fn sqlite3_bind_text(
        statement: *mut SqliteStmt,
        index: c_int,
        value: *const c_char,
        n: c_int,
        destructor: *const c_void,
    ) -> c_int;
    fn sqlite3_step(statement: *mut SqliteStmt) -> c_int;
    fn sqlite3_finalize(statement: *mut SqliteStmt) -> c_int;
    fn sqlite3_column_type(statement: *mut SqliteStmt, column: c_int) -> c_int;
    fn sqlite3_column_int64(statement: *mut SqliteStmt, column: c_int) -> i64;
    fn sqlite3_column_text(statement: *mut SqliteStmt, column: c_int) -> *const c_uchar;
    fn sqlite3_column_bytes(statement: *mut SqliteStmt, column: c_int) -> c_int;
}

struct Database(*mut Sqlite3);

impl Drop for Database {
    fn drop(&mut self) {
        unsafe {
            sqlite3_close_v2(self.0);
        }
    }
}

struct Statement(*mut SqliteStmt);

impl Drop for Statement {
    fn drop(&mut self) {
        unsafe {
            sqlite3_finalize(self.0);
        }
    }
}

struct Window {
    start: DateTime<FixedOffset>,
    end: DateTime<FixedOffset>,
    lower: i64,
    upper: i64,
    scan_lower: i64,
    scan_upper: i64,
}

fn parse_window(args: &[std::ffi::OsString]) -> Option<Window> {
    if args.len() != 5
        || args[0].to_str()? != "alerts"
        || args[1].to_str()? != "--from"
        || args[3].to_str()? != "--to"
    {
        return None;
    }

    let start = DateTime::parse_from_rfc3339(args[2].to_str()?).ok()?;
    let end = DateTime::parse_from_rfc3339(args[4].to_str()?).ok()?;
    if end <= start {
        return None;
    }

    Some(Window {
        lower: python_epoch_second(&start)?,
        upper: python_epoch_second(&end)?,
        scan_lower: python_epoch_second(
            &start.checked_sub_signed(Duration::seconds(ALERT_EVENT_HORIZON_SECONDS))?,
        )?,
        scan_upper: python_epoch_second(
            &end.checked_add_signed(Duration::seconds(ALERT_EVENT_HORIZON_SECONDS))?,
        )?,
        start,
        end,
    })
}

fn python_epoch_second(value: &DateTime<FixedOffset>) -> Option<i64> {
    let timestamp = value.timestamp();
    if timestamp < 0 && value.timestamp_subsec_nanos() != 0 {
        timestamp.checked_add(1)
    } else {
        Some(timestamp)
    }
}

fn state_database_path() -> Option<PathBuf> {
    let state = match env::var_os("XDG_STATE_HOME") {
        Some(path) => PathBuf::from(path),
        None => PathBuf::from(env::var_os("HOME")?).join(".local/state"),
    };
    Some(state.join("orchard").join("cache.db"))
}

fn c_string_path(path: &OsStr) -> Option<CString> {
    CString::new(path.as_bytes()).ok()
}

fn open_database() -> Option<Database> {
    let path = state_database_path()?;
    let filename = c_string_path(path.as_os_str())?;
    let mut db = std::ptr::null_mut();
    let status = unsafe {
        sqlite3_open_v2(
            filename.as_ptr(),
            &mut db,
            SQLITE_OPEN_READONLY,
            std::ptr::null(),
        )
    };
    if status != SQLITE_OK || db.is_null() {
        if !db.is_null() {
            unsafe { sqlite3_close_v2(db) };
        }
        return None;
    }
    unsafe {
        sqlite3_busy_timeout(db, 250);
    }
    Some(Database(db))
}

fn execute(db: &Database, sql: &str) -> bool {
    let Ok(sql) = CString::new(sql) else {
        return false;
    };
    unsafe {
        sqlite3_exec(
            db.0,
            sql.as_ptr(),
            None,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        ) == SQLITE_OK
    }
}

fn prepare(db: &Database, sql: &str) -> Option<Statement> {
    let sql = CString::new(sql).ok()?;
    let mut statement = std::ptr::null_mut();
    let status =
        unsafe { sqlite3_prepare_v2(db.0, sql.as_ptr(), -1, &mut statement, std::ptr::null_mut()) };
    (status == SQLITE_OK && !statement.is_null()).then_some(Statement(statement))
}

fn integer_column(statement: &Statement, index: c_int) -> Option<i64> {
    if unsafe { sqlite3_column_type(statement.0, index) } != SQLITE_INTEGER {
        return None;
    }
    Some(unsafe { sqlite3_column_int64(statement.0, index) })
}

fn text_column(statement: &Statement, index: c_int) -> Option<String> {
    if unsafe { sqlite3_column_type(statement.0, index) } != SQLITE_TEXT {
        return None;
    }
    let ptr = unsafe { sqlite3_column_text(statement.0, index) };
    let len = unsafe { sqlite3_column_bytes(statement.0, index) };
    if ptr.is_null() || len < 0 {
        return None;
    }
    let bytes = unsafe { std::slice::from_raw_parts(ptr, len as usize) };
    std::str::from_utf8(bytes).ok().map(str::to_owned)
}

fn timezone_fingerprint() -> Option<String> {
    let tz_value = match env::var_os("TZ") {
        Some(value) => value.into_string().ok()?,
        None => String::new(),
    };

    let localtime_bytes = std::fs::read("/etc/localtime").ok();
    let localtime_hash = localtime_bytes
        .as_deref()
        .map(sha256_hex)
        .unwrap_or_default();
    let local_zone = std::fs::canonicalize("/etc/localtime")
        .ok()
        .and_then(|path| path.to_str().map(str::to_owned))
        .and_then(|path| {
            path.split_once("/zoneinfo/")
                .map(|(_, zone)| zone.to_owned())
        })
        .unwrap_or_else(|| tz_value.trim_start_matches(':').to_owned());

    let input = format!("orchard-alert-tz-v1\0{local_zone}\0{tz_value}\0{localtime_hash}");
    Some(sha256_hex(input.as_bytes()))
}

fn fast_alerts(window: &Window, started: Instant) -> Option<String> {
    let expected_fingerprint = timezone_fingerprint()?;
    let db = open_database()?;
    if !execute(&db, "BEGIN") {
        return None;
    }

    let meta = prepare(
        &db,
        "SELECT source_revision,built_revision,schema_version,projection_version,\
         timezone_fingerprint,coverage_from,coverage_to \
         FROM alert_cache_meta WHERE id=1",
    )?;
    if unsafe { sqlite3_step(meta.0) } != SQLITE_ROW {
        return None;
    }

    let source_revision = integer_column(&meta, 0)?;
    let built_revision = integer_column(&meta, 1)?;
    let schema_version = integer_column(&meta, 2)?;
    let projection_version = integer_column(&meta, 3)?;
    let fingerprint = text_column(&meta, 4)?;
    let coverage_from = integer_column(&meta, 5)?;
    let coverage_to = integer_column(&meta, 6)?;

    if source_revision != built_revision
        || schema_version != 1
        || projection_version != 1
        || fingerprint != expected_fingerprint
        || window.lower < coverage_from
        || window.upper > coverage_to
    {
        return None;
    }
    drop(meta);

    let rows = prepare(
        &db,
        "SELECT o.href,o.payload FROM alert_occurrences AS o \
         JOIN calendars AS c ON c.url=o.calendar \
         WHERE c.enabled=1 AND o.alert_at>=?1 AND o.alert_at<?2 \
         AND (o.source_open_ended=1 OR \
              (o.source_last_end>=?3 AND o.source_first_start<?4)) \
         ORDER BY o.alert_at,o.id",
    )?;
    unsafe {
        if sqlite3_bind_int64(rows.0, 1, window.lower) != SQLITE_OK
            || sqlite3_bind_int64(rows.0, 2, window.upper) != SQLITE_OK
            || sqlite3_bind_int64(rows.0, 3, window.scan_lower) != SQLITE_OK
            || sqlite3_bind_int64(rows.0, 4, window.scan_upper) != SQLITE_OK
        {
            return None;
        }
    }

    let mut alerts: Vec<Value> = Vec::new();
    let mut hrefs = HashSet::new();
    loop {
        match unsafe { sqlite3_step(rows.0) } {
            SQLITE_ROW => {
                let href = text_column(&rows, 0)?;
                let payload = text_column(&rows, 1)?;
                let alert: Value = serde_json::from_str(&payload).ok()?;
                if !alert.is_object() {
                    return None;
                }
                hrefs.insert(href);
                alerts.push(alert);
            }
            SQLITE_DONE => break,
            _ => return None,
        }
    }
    drop(rows);
    if !execute(&db, "COMMIT") {
        return None;
    }

    alerts.sort_by(|left, right| {
        let left_at = left.get("at").and_then(Value::as_str).unwrap_or("");
        let right_at = right.get("at").and_then(Value::as_str).unwrap_or("");
        let left_title = left.get("title").and_then(Value::as_str).unwrap_or("");
        let right_title = right.get("title").and_then(Value::as_str).unwrap_or("");
        let left_id = left.get("id").and_then(Value::as_str).unwrap_or("");
        let right_id = right.get("id").and_then(Value::as_str).unwrap_or("");
        (left_at, left_title, left_id).cmp(&(right_at, right_title, right_id))
    });

    let response = json!({
        "ok": true,
        "alerts": alerts,
        "parsed": hrefs.len(),
        "range": {
            "start": python_isoformat(&window.start),
            "end": python_isoformat(&window.end),
        },
        "elapsed": ((started.elapsed().as_secs_f64() * 1000.0).round() / 1000.0),
    });
    serde_json::to_string(&response).ok()
}

fn python_isoformat(value: &DateTime<FixedOffset>) -> String {
    let mut result = value.format("%Y-%m-%dT%H:%M:%S").to_string();
    let micros = value.timestamp_subsec_nanos() / 1_000;
    if micros != 0 {
        result.push('.');
        result.push_str(&format!("{micros:06}"));
    }

    let offset = value.offset().local_minus_utc();
    let sign = if offset < 0 { '-' } else { '+' };
    let absolute = offset.unsigned_abs();
    result.push_str(&format!(
        "{sign}{:02}:{:02}",
        absolute / 3_600,
        absolute % 3_600 / 60
    ));
    result
}

// ---- fast `events` path ---------------------------------------------------
//
// The month/week/day window read: the hot path of a panel open and of every
// page turn. It uses the same read-only SQLite cache and the same libical C
// library the Python helper does, so a panel never has to pay for a whole
// CPython + GI interpreter to list a window.
//
// Nothing calendar-semantic is reimplemented. Recurrence, RDATE/EXDATE and
// time-zone resolution all run through the same libical calls the Python
// `expand()` drives (`i_cal_component_foreach_recurrence`, the ICal time-zone
// helpers, `as_timet_with_zone`), so the spans come back identically. Only
// the pure-data presentation layer — field extraction, meeting-link detection,
// ISO formatting, sorting — is ported. Any input this path does not understand
// returns None and the process execs the Python helper, so it never does worse
// than today.

type CalObject = c_void; // ICalComponent / ICalProperty / ICalValue / ICalTime / ICalTimezone

#[repr(C)]
struct Tm {
    tm_sec: c_int,
    tm_min: c_int,
    tm_hour: c_int,
    tm_mday: c_int,
    tm_mon: c_int,
    tm_year: c_int,
    tm_wday: c_int,
    tm_yday: c_int,
    tm_isdst: c_int,
    tm_gmtoff: i64,
    tm_zone: *const c_char,
}

#[link(name = "ical-glib")]
#[link(name = "ical")]
#[link(name = "gobject-2.0")]
#[link(name = "glib-2.0")]
extern "C" {
    fn i_cal_component_new_from_string(s: *const c_char) -> *mut CalObject;
    fn i_cal_component_free(c: *mut CalObject);
    fn i_cal_component_get_first_component(c: *mut CalObject, kind: c_int) -> *mut CalObject;
    fn i_cal_component_get_next_component(c: *mut CalObject, kind: c_int) -> *mut CalObject;
    fn i_cal_component_get_first_property(c: *mut CalObject, kind: c_int) -> *mut CalObject;
    fn i_cal_property_get_value_as_string(p: *mut CalObject) -> *mut c_char;
    fn i_cal_component_get_dtstart(c: *mut CalObject) -> *mut CalObject;
    fn i_cal_component_get_dtend(c: *mut CalObject) -> *mut CalObject;
    fn i_cal_component_get_summary(c: *mut CalObject) -> *const c_char;
    fn i_cal_component_get_uid(c: *mut CalObject) -> *const c_char;
    fn i_cal_component_get_description(c: *mut CalObject) -> *const c_char;
    fn i_cal_component_get_location(c: *mut CalObject) -> *const c_char;
    fn i_cal_component_get_recurrenceid(c: *mut CalObject) -> *mut CalObject;
    fn i_cal_component_get_timezone(c: *mut CalObject, tzid: *const c_char) -> *mut CalObject;
    fn i_cal_component_get_span(c: *mut CalObject) -> *mut CalObject;
    fn i_cal_component_foreach_recurrence(
        c: *mut CalObject,
        start: *mut CalObject,
        end: *mut CalObject,
        cb: extern "C" fn(*mut CalObject, *mut CalObject, *mut c_void),
        data: *mut c_void,
    );
    fn i_cal_time_is_date(t: *mut CalObject) -> c_int;
    fn i_cal_time_is_utc(t: *mut CalObject) -> c_int;
    fn i_cal_time_is_null_time(t: *mut CalObject) -> c_int;
    fn i_cal_time_as_timet(t: *mut CalObject) -> i64;
    fn i_cal_time_as_timet_with_zone(t: *mut CalObject, z: *mut CalObject) -> i64;
    fn i_cal_time_get_timezone(t: *mut CalObject) -> *mut CalObject;
    fn i_cal_time_as_ical_string(t: *mut CalObject) -> *mut c_char;
    fn i_cal_time_new_from_timet_with_zone(v: i64, is_date: c_int, z: *mut CalObject) -> *mut CalObject;
    fn i_cal_timezone_get_location(z: *mut CalObject) -> *const c_char;
    fn i_cal_timezone_get_builtin_timezone(name: *const c_char) -> *mut CalObject;
    fn i_cal_time_span_get_start(s: *mut CalObject) -> i64;
    fn i_cal_time_span_get_end(s: *mut CalObject) -> i64;
    fn g_free(p: *mut c_void);
}

extern "C" {
    fn localtime_r(t: *const i64, result: *mut Tm) -> *mut Tm;
}

// Raw iCalendar component/property enums (identical to the GLib wrappers);
// the numbers were verified against the installed 4.0.5 headers.
const IC_VEVENT: c_int = 4;
const IC_URL: c_int = 88;
const IC_CONFERENCE: c_int = 120;

const SQLITE_TRANSIENT: isize = -1;

fn bind_text(statement: &Statement, index: c_int, text: &str) -> Option<()> {
    let value = CString::new(text).ok()?;
    let status = unsafe {
        sqlite3_bind_text(
            statement.0,
            index,
            value.as_ptr(),
            -1,
            SQLITE_TRANSIENT as *const c_void,
        )
    };
    if status == SQLITE_OK {
        Some(())
    } else {
        None
    }
}

fn cstr_to_string(p: *const c_char) -> String {
    if p.is_null() {
        return String::new();
    }
    std::str::from_utf8(unsafe { std::ffi::CStr::from_ptr(p).to_bytes() })
        .map(str::to_owned)
        .unwrap_or_default()
}

// `get_first_property(kind)` + value text, or "". The value is a fresh
// `gchar*` (independent of the component) and is freed here.
fn property_string(comp: *mut CalObject, kind: c_int) -> String {
    let prop = unsafe { i_cal_component_get_first_property(comp, kind) };
    if prop.is_null() {
        return String::new();
    }
    let p = unsafe { i_cal_property_get_value_as_string(prop) };
    let text = cstr_to_string(p as *const c_char);
    if !p.is_null() {
        unsafe { g_free(p as *mut c_void) };
    }
    text
}

fn is_host_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '.' || c == '-' || c == '_'
}

// Trim the trailing punctuation a URL picks up from the surrounding prose.
fn strip_meeting(mut s: String) -> String {
    while s.chars().last().map_or(false, |c| matches!(c, '.' | ',' | '>' | ')')) {
        s.pop();
    }
    s
}

// `https://[\w.-]*<host>/<token>` matched case-insensitively, returning the
// URL token. `allow_sub` permits a subdomain before the host (Zoom, Webex);
// Meet and Teams match their host exactly.
fn meeting_host(field: &str, host: &str, allow_sub: bool) -> Option<String> {
    let chars: Vec<char> = field.chars().collect();
    let lower: Vec<char> = chars.iter().map(|c| c.to_ascii_lowercase()).collect();
    let scheme: String = "https://".to_string();
    let dom: String = host.to_lowercase();
    let n = chars.len();
    let mut i = 0usize;
    while n.saturating_sub(i) >= scheme.len() {
        let scheme_text: String = lower[i..i + scheme.len()].iter().collect();
        if scheme_text != scheme {
            i += 1;
            continue;
        }
        let rest = i + scheme.len();
        let mut he = rest;
        while he < n && is_host_char(lower[he]) {
            he += 1;
        }
        let host_text: String = lower[rest..he].iter().collect();
        let host_ok = if allow_sub {
            host_text.ends_with(dom.as_str())
        } else {
            host_text == dom
        };
        if host_ok && he < n && lower[he] == '/' {
            let mut j = he + 1;
            while j < n && !chars[j].is_whitespace() {
                j += 1;
            }
            if j > he + 1 {
                let url: String = chars[i..j].iter().collect();
                return Some(strip_meeting(url));
            }
        }
        i = rest;
    }
    None
}

// Mirror of `meeting_link`: fields in the order they carry the call, then the
// services in recogniser order; first hit wins. A bare conference value with
// no recognised host is still how the call is joined.
fn meeting_link(conference: &str, url: &str, location: &str, description: &str) -> (String, String) {
    for field in [conference, url, location, description] {
        if field.is_empty() {
            continue;
        }
        for (kind, host, sub) in [
            ("zoom", "zoom.us", true),
            ("meet", "meet.google.com", false),
            ("teams", "teams.microsoft.com", false),
            ("webex", "webex.com", true),
        ] {
            if let Some(link) = meeting_host(field, host, sub) {
                return (link, kind.to_string());
            }
        }
    }
    if !conference.is_empty() {
        return (conference.to_string(), "conference".to_string());
    }
    (String::new(), String::new())
}

// The machine's UTC offset at a particular instant, from the C library's zone
// (the same /etc/localtime or TZ the Python `system_zone()` resolves), so a
// DST boundary lands on the right hour the way `datetime.fromtimestamp` does.
fn local_offset_at(epoch: i64) -> Option<i32> {
    let mut tm: Tm = unsafe { std::mem::zeroed() };
    let p = unsafe { localtime_r(&epoch, &mut tm) };
    if p.is_null() {
        return None;
    }
    Some(unsafe { (*p).tm_gmtoff } as i32)
}

// `datetime.fromtimestamp(seconds, LOCAL_TZ).isoformat()` for a whole-second
// instant, in the machine's own zone with its offset.
fn local_iso(epoch: i64) -> Option<String> {
    let offset = local_offset_at(epoch)?;
    let base = DateTime::from_timestamp(epoch, 0)?.naive_utc();
    let local = base + Duration::seconds(offset as i64);
    let sign = if offset < 0 { '-' } else { '+' };
    let abs = offset.unsigned_abs();
    Some(format!(
        "{}{}{:02}:{:02}",
        local.format("%Y-%m-%dT%H:%M:%S"),
        sign,
        abs / 3_600,
        (abs % 3_600) / 60
    ))
}

// An all-day event is a date, not an instant: libical reports its span at
// midnight UTC, so the date is read back in UTC the way `floating_date` does.
fn floating_date(epoch: i64) -> Option<String> {
    Some(DateTime::from_timestamp(epoch, 0)?.format("%Y-%m-%d").to_string())
}

// Mirror of `parse_when`: a bare date is local midnight; a datetime keeps its
// own offset (or Z), a naive one is local time.
fn parse_when_py(s: &str) -> Option<(i64, String)> {
    if s.len() == 10 {
        let day = NaiveDate::parse_from_str(s, "%Y-%m-%d").ok()?;
        let midnight = day.and_hms_opt(0, 0, 0)?;
        let t0 = midnight.and_utc().timestamp();
        let offset = local_offset_at(t0)? as i64;
        let epoch = t0 - offset;
        return Some((epoch, local_iso(epoch)?));
    }
    let replaced = s.replace('Z', "+00:00");
    let mut candidates: Vec<&str> = vec![s];
    if replaced != s {
        candidates.push(&replaced);
    }
    for candidate in candidates {
        if let Ok(dt) = DateTime::parse_from_rfc3339(candidate) {
            return Some((dt.timestamp(), python_isoformat(&dt)));
        }
    }
    if let Ok(naive) = NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S") {
        let t0 = naive.and_utc().timestamp();
        let offset = local_offset_at(t0)? as i64;
        let epoch = t0 - offset;
        return Some((epoch, local_iso(epoch)?));
    }
    None
}

// `i_cal_component_get_timezone` builds a zone from the object's own
// VTIMEZONE; a shared built-in covers a TZID the file only named.
fn resolve_zone(root: *mut CalObject, name: &str) -> Option<*mut CalObject> {
    if name.is_empty() {
        return None;
    }
    let name_c = CString::new(name).ok()?;
    let zone = unsafe { i_cal_component_get_timezone(root, name_c.as_ptr()) };
    if !zone.is_null() {
        return Some(zone);
    }
    let builtin = unsafe { i_cal_timezone_get_builtin_timezone(name_c.as_ptr()) };
    if builtin.is_null() {
        None
    } else {
        Some(builtin)
    }
}

// Resolve an ICalTime to a real instant, TZID respected, exactly as the Python
// `absolute_time`: date/UTC read raw, otherwise through the named zone.
fn absolute_time(root: *mut CalObject, t: *mut CalObject) -> Option<i64> {
    if t.is_null() || unsafe { i_cal_time_is_null_time(t) } != 0 {
        return None;
    }
    if unsafe { i_cal_time_is_date(t) } != 0 || unsafe { i_cal_time_is_utc(t) } != 0 {
        return Some(unsafe { i_cal_time_as_timet(t) });
    }
    let tz = unsafe { i_cal_time_get_timezone(t) };
    if !tz.is_null() {
        let name = cstr_to_string(unsafe { i_cal_timezone_get_location(tz) });
        if !name.is_empty() {
            if let Some(zone) = resolve_zone(root, &name) {
                return Some(unsafe { i_cal_time_as_timet_with_zone(t, zone) });
            }
        }
    }
    Some(unsafe { i_cal_time_as_timet(t) })
}

// `(start, end)` of one component as real instants, mirroring `span_of` and
// its DTEND fallback (a DURATION, or a zero-length event).
fn span_of(root: *mut CalObject, comp: *mut CalObject) -> Option<(i64, i64)> {
    let begin = absolute_time(root, unsafe { i_cal_component_get_dtstart(comp) });
    let finish = absolute_time(root, unsafe { i_cal_component_get_dtend(comp) });
    let raw = || -> Option<(i64, i64)> {
        let span = unsafe { i_cal_component_get_span(comp) };
        if span.is_null() {
            return None;
        }
        Some((unsafe { i_cal_time_span_get_start(span) }, unsafe { i_cal_time_span_get_end(span) }))
    };
    match (begin, finish) {
        (None, _) => raw(),
        (Some(b), None) => Some((b, b + (raw()?.1 - raw()?.0).max(0))),
        (Some(b), Some(f)) => Some((b, f)),
    }
}

// The one event the UI needs: base fields plus when, all-day-ness and rid.
fn occurrence(base: &Value, all_day: bool, begin: i64, finish: i64, rid: &str) -> Option<Value> {
    let (start, end) = if all_day {
        (floating_date(begin)?, floating_date(finish)?)
    } else {
        (local_iso(begin)?, local_iso(finish)?)
    };
    let mut v = base.clone();
    let obj = v.as_object_mut()?;
    obj.insert("allDay".into(), json!(all_day));
    obj.insert("rid".into(), json!(rid.to_string()));
    obj.insert("start".into(), json!(start));
    obj.insert("end".into(), json!(end));
    Some(v)
}

// Mirror of `base_fields`, in the key order the UI relies on.
fn base_fields(comp: *mut CalObject, calendar_url: &str, name: &str, color: &str) -> Value {
    let uid = cstr_to_string(unsafe { i_cal_component_get_uid(comp) });
    let title = cstr_to_string(unsafe { i_cal_component_get_summary(comp) });
    let location = cstr_to_string(unsafe { i_cal_component_get_location(comp) });
    let description = cstr_to_string(unsafe { i_cal_component_get_description(comp) });
    let conference = property_string(comp, IC_CONFERENCE);
    let url = property_string(comp, IC_URL);
    let (link, kind) = meeting_link(&conference, &url, &location, &description);
    json!({
        "uid": uid,
        "title": title,
        "location": location,
        "calendar": name,
        "calendarUrl": calendar_url,
        "color": color,
        "meetingUrl": link,
        "meetingKind": kind,
        "conference": conference,
    })
}

// What the `foreach_recurrence` C callback can read: the sink it appends to,
// the master's base fields, its all-day-ness, the override slots, the href and
// the object's pending flag.
struct RecurCtx {
    out: *mut Vec<Value>,
    base: *const Value,
    all_day: bool,
    overrides: *const HashSet<i64>,
    href: String,
    pending: bool,
}

extern "C" fn recurrence_collect(_comp: *mut CalObject, span: *mut CalObject, data: *mut c_void) {
    let ctx = unsafe { &*(data as *const RecurCtx) };
    let begin = unsafe { i_cal_time_span_get_start(span) };
    if unsafe { (*ctx.overrides).contains(&begin) } {
        return; // a RECURRENCE-ID override takes this slot
    }
    let finish = unsafe { i_cal_time_span_get_end(span) };
    let base = unsafe { &*ctx.base };
    let mut ev = match occurrence(base, ctx.all_day, begin, finish, "") {
        Some(o) => o,
        None => return,
    };
    if !ctx.href.is_empty() {
        if let Some(obj) = ev.as_object_mut() {
            obj.insert("href".into(), json!(ctx.href.clone()));
        }
    }
    if ctx.pending {
        if let Some(obj) = ev.as_object_mut() {
            obj.insert("pending".into(), json!(true));
        }
    }
    unsafe { (*ctx.out).push(ev) };
}

// One calendar object into the window: the master via `foreach_recurrence`,
// then each RECURRENCE-ID override landing inside `[lower, upper)`. Transient
// per-row ICal objects are not freed — this is a one-shot process and releasing
// the root is the one thing that must happen.
fn expand_object(
    ics: &str,
    calendar_url: &str,
    name: &str,
    color: &str,
    href: &str,
    pending: bool,
    lower: i64,
    upper: i64,
    utc_zone: Option<*mut CalObject>,
    out: &mut Vec<Value>,
) {
    let ics_c = match CString::new(ics) {
        Ok(v) => v,
        Err(_) => return,
    };
    let root = unsafe { i_cal_component_new_from_string(ics_c.as_ptr()) };
    if root.is_null() {
        return;
    }

    // Split master from overrides and remember the override slots.
    let mut master: *mut CalObject = std::ptr::null_mut();
    let mut overrides: Vec<(*mut CalObject, i64)> = Vec::new();
    unsafe {
        let mut c = i_cal_component_get_first_component(root, IC_VEVENT);
        while !c.is_null() {
            let rid_time = i_cal_component_get_recurrenceid(c);
            if !rid_time.is_null() && i_cal_time_is_null_time(rid_time) == 0 {
                if let Some(at) = absolute_time(root, rid_time) {
                    overrides.push((c, at));
                }
            } else if master.is_null() {
                master = c;
            }
            c = i_cal_component_get_next_component(root, IC_VEVENT);
        }
    }

    if !master.is_null() {
        let all_day = {
            let dt = unsafe { i_cal_component_get_dtstart(master) };
            !dt.is_null() && unsafe { i_cal_time_is_date(dt) } != 0
        };
        let base = base_fields(master, calendar_url, name, color);
        let override_set: HashSet<i64> = overrides.iter().map(|&(_, at)| at).collect();
        if let Some(utc_zone) = utc_zone {
            let ws = unsafe { i_cal_time_new_from_timet_with_zone(lower, 0, utc_zone) };
            let we = unsafe { i_cal_time_new_from_timet_with_zone(upper, 0, utc_zone) };
            if !ws.is_null() && !we.is_null() {
                let ctx = RecurCtx {
                    out: out as *mut Vec<Value>,
                    base: &base,
                    all_day,
                    overrides: &override_set,
                    href: href.to_string(),
                    pending,
                };
                unsafe {
                    i_cal_component_foreach_recurrence(
                        master,
                        ws,
                        we,
                        recurrence_collect,
                        &ctx as *const RecurCtx as *mut c_void,
                    )
                };
            }
        }
    }

    // Overrides that fall inside the requested range, mirroring the Python loop.
    for (comp, _rid_at) in &overrides {
        let comp = *comp;
        let (begin, finish) = match span_of(root, comp) {
            Some(v) => v,
            None => continue,
        };
        if finish < lower || begin >= upper {
            continue;
        }
        let rid_time = unsafe { i_cal_component_get_recurrenceid(comp) };
        let rid = if !rid_time.is_null() {
            let p = unsafe { i_cal_time_as_ical_string(rid_time) };
            let text = cstr_to_string(p as *const c_char);
            if !p.is_null() {
                unsafe { g_free(p as *mut c_void) };
            }
            text
        } else {
            String::new()
        };
        let all_day = {
            let dt = unsafe { i_cal_component_get_dtstart(comp) };
            !dt.is_null() && unsafe { i_cal_time_is_date(dt) } != 0
        };
        let base = base_fields(comp, calendar_url, name, color);
        if let Some(mut ev) = occurrence(&base, all_day, begin, finish, &rid) {
            if !href.is_empty() {
                if let Some(obj) = ev.as_object_mut() {
                    obj.insert("href".into(), json!(href));
                }
            }
            if pending {
                if let Some(obj) = ev.as_object_mut() {
                    obj.insert("pending".into(), json!(true));
                }
            }
            out.push(ev);
        }
    }

    unsafe { i_cal_component_free(root) };
}

fn objects_count(db: &Database) -> Option<i64> {
    let stmt = prepare(db, "SELECT COUNT(*) FROM objects")?;
    if unsafe { sqlite3_step(stmt.0) } != SQLITE_ROW {
        return None;
    }
    Some(integer_column(&stmt, 0)?)
}

fn events_response(
    events: Vec<Value>,
    parsed: i64,
    objects: i64,
    start_label: &str,
    end_label: &str,
    started: Instant,
) -> String {
    let response = json!({
        "ok": true,
        "events": events,
        "parsed": parsed,
        "objects": objects,
        "range": { "start": start_label, "end": end_label },
        "elapsed": (started.elapsed().as_secs_f64() * 100.0).round() / 100.0,
    });
    serde_json::to_string(&response).unwrap_or_else(|_| "{}".to_string())
}

// `events --from <date> --to <date>`: the exact shape the panel uses.
fn fast_events(args: &[std::ffi::OsString]) -> Option<String> {
    let started = Instant::now();
    if args.len() != 5 {
        return None;
    }
    if args[0].to_str()? != "events" || args[1].to_str()? != "--from" || args[3].to_str()? != "--to" {
        return None;
    }
    let (lower, start_label) = parse_when_py(args[2].to_str()?)?;
    let (upper, end_label) = parse_when_py(args[4].to_str()?)?;
    if upper <= lower {
        return None;
    }

    let db = open_database()?;

    // Enabled calendars, keyed by url as in `visible_calendars`.
    let cals = prepare(&db, "SELECT url,name,color FROM calendars WHERE enabled=1")?;
    let mut names: std::collections::HashMap<String, (String, String)> = std::collections::HashMap::new();
    let mut wanted: Vec<String> = Vec::new();
    while unsafe { sqlite3_step(cals.0) } == SQLITE_ROW {
        let url = text_column(&cals, 0)?;
        wanted.push(url.clone());
        names.insert(url, (text_column(&cals, 1)?, text_column(&cals, 2)?));
    }
    drop(cals);

    let objects = objects_count(&db)?;
    if wanted.is_empty() {
        return Some(events_response(Vec::new(), 0, objects, &start_label, &end_label, started));
    }

    // Objects that can reach the window, restricted to the enabled set —
    // `window_rows` with `calendar IN (wanted)`.
    let list = vec!['?'; wanted.len()].iter().collect::<String>();
    let sql = format!(
        "SELECT url,calendar,ics,pending FROM objects \
         WHERE (open_ended=1 OR (last_end>=?1 AND first_start<?2)) \
         AND calendar IN ({list})",
    );
    let rows = prepare(&db, &sql)?;
    if unsafe { sqlite3_bind_int64(rows.0, 1, lower) } != SQLITE_OK
        || unsafe { sqlite3_bind_int64(rows.0, 2, upper) } != SQLITE_OK
    {
        return None;
    }
    for (index, url) in wanted.iter().enumerate() {
        bind_text(&rows, 3 + index as c_int, url)?;
    }

    // The shared UTC zone the window bounds pass to `foreach_recurrence`.
    let utc_name = CString::new("UTC").ok()?;
    let zone_ptr = unsafe { i_cal_timezone_get_builtin_timezone(utc_name.as_ptr()) };
    let utc_zone = if zone_ptr.is_null() { None } else { Some(zone_ptr) };

    let mut events: Vec<Value> = Vec::new();
    let mut parsed: i64 = 0;
    loop {
        match unsafe { sqlite3_step(rows.0) } {
            SQLITE_ROW => {
                let href = text_column(&rows, 0)?;
                let calendar = text_column(&rows, 1)?;
                let ics = text_column(&rows, 2)?;
                let pending = integer_column(&rows, 3).unwrap_or(0) != 0;
                let (name, color) = match names.get(&calendar) {
                    Some(entry) => (entry.0.clone(), entry.1.clone()),
                    None => continue, // guarded by the IN() filter, but be safe
                };
                parsed += 1;
                expand_object(
                    &ics, &calendar, &name, &color, &href, pending, lower, upper, utc_zone, &mut events,
                );
            }
            SQLITE_DONE => break,
            _ => return None,
        }
    }
    drop(rows);

    // By day, all-day first (they head the band in every view), then by time —
    // a stable sort, matching the Python key `(start[:10], not allDay, start)`.
    events.sort_by(|a, b| {
        let key = |v: &Value| {
            let start = v.get("start").and_then(Value::as_str).unwrap_or("");
            let ad = v.get("allDay").and_then(Value::as_bool).unwrap_or(false);
            (start.chars().take(10).collect::<String>(), !ad, start.to_string())
        };
        key(a).cmp(&key(b))
    });

    Some(events_response(events, parsed, objects, &start_label, &end_label, started))
}

fn python_helper_path() -> Option<PathBuf> {
    let executable = env::current_exe().ok()?;
    Some(executable.parent()?.join("orchard-helper"))
}

fn exec_python(args: &[std::ffi::OsString]) -> ! {
    let helper = python_helper_path();
    let Some(helper) = helper else {
        eprintln!("orchard-native: cannot locate the Python helper");
        std::process::exit(127);
    };
    let error = Command::new("python3")
        .arg(helper)
        .args(args)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .exec();
    eprintln!("orchard-native: could not exec Python helper: {error}");
    std::process::exit(127);
}

fn main() {
    let args: Vec<_> = env::args_os().skip(1).collect();
    if let Some(result) = fast_events(&args) {
        println!("{result}");
        return;
    }
    if let Some(window) = parse_window(&args) {
        if let Some(result) = fast_alerts(&window, Instant::now()) {
            println!("{result}");
            return;
        }
    }
    exec_python(&args);
}

fn sha256_hex(data: &[u8]) -> String {
    const INITIAL: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];

    let bit_length = (data.len() as u64).wrapping_mul(8);
    let mut padded = Vec::with_capacity((data.len() + 72) & !63);
    padded.extend_from_slice(data);
    padded.push(0x80);
    while padded.len() % 64 != 56 {
        padded.push(0);
    }
    padded.extend_from_slice(&bit_length.to_be_bytes());

    let mut state = INITIAL;
    for block in padded.chunks_exact(64) {
        let mut words = [0_u32; 64];
        for (index, chunk) in block.chunks_exact(4).take(16).enumerate() {
            words[index] = u32::from_be_bytes(chunk.try_into().expect("four-byte SHA word"));
        }
        for index in 16..64 {
            let x = words[index - 15];
            let y = words[index - 2];
            let s0 = x.rotate_right(7) ^ x.rotate_right(18) ^ (x >> 3);
            let s1 = y.rotate_right(17) ^ y.rotate_right(19) ^ (y >> 10);
            words[index] = words[index - 16]
                .wrapping_add(s0)
                .wrapping_add(words[index - 7])
                .wrapping_add(s1);
        }

        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = state;
        for index in 0..64 {
            let sum1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let choose = (e & f) ^ ((!e) & g);
            let first = h
                .wrapping_add(sum1)
                .wrapping_add(choose)
                .wrapping_add(K[index])
                .wrapping_add(words[index]);
            let sum0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let majority = (a & b) ^ (a & c) ^ (b & c);
            let second = sum0.wrapping_add(majority);
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(first);
            d = c;
            c = b;
            b = a;
            a = first.wrapping_add(second);
        }
        for (slot, value) in state.iter_mut().zip([a, b, c, d, e, f, g, h]) {
            *slot = slot.wrapping_add(value);
        }
    }

    let mut hex = String::with_capacity(64);
    for word in state {
        hex.push_str(&format!("{word:08x}"));
    }
    hex
}

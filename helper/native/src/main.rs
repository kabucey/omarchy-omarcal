use chrono::{DateTime, Duration, FixedOffset};
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
    Some(state.join("omarcal").join("cache.db"))
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

    let input = format!("omarcal-alert-tz-v1\0{local_zone}\0{tz_value}\0{localtime_hash}");
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

fn python_helper_path() -> Option<PathBuf> {
    let executable = env::current_exe().ok()?;
    Some(executable.parent()?.join("omarcal-helper"))
}

fn exec_python(args: &[std::ffi::OsString]) -> ! {
    let helper = python_helper_path();
    let Some(helper) = helper else {
        eprintln!("omarcal-native: cannot locate the Python helper");
        std::process::exit(127);
    };
    let error = Command::new("python3")
        .arg(helper)
        .args(args)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .exec();
    eprintln!("omarcal-native: could not exec Python helper: {error}");
    std::process::exit(127);
}

fn main() {
    let args: Vec<_> = env::args_os().skip(1).collect();
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

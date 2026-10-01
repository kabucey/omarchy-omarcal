import QtQuick
import QtTest
import "../Logic.js" as Logic

// Drawing a new event by dragging down the day or week rail: the pointer read
// in quarter hours, the span it draws, and the event that span becomes.
TestCase {
  name: "dragnew"

  readonly property var wholeDay: Logic.dayWindow(0, 24)

  function test_minute_snaps_to_the_nearest_quarter() {
    // 40px an hour: 10px is a quarter.
    compare(Logic.dragMinute(0, 40, wholeDay), 0)
    compare(Logic.dragMinute(360, 40, wholeDay), 9 * 60)
    compare(Logic.dragMinute(364, 40, wholeDay), 9 * 60)
    compare(Logic.dragMinute(366, 40, wholeDay), 9 * 60 + 15)
  }

  function test_minute_stays_inside_the_rail() {
    var work = Logic.dayWindow(8, 18)
    compare(Logic.dragMinute(-50, 40, work), 8 * 60)
    compare(Logic.dragMinute(0, 40, work), 8 * 60)
    compare(Logic.dragMinute(40, 40, work), 9 * 60)
    compare(Logic.dragMinute(10000, 40, work), 18 * 60)
  }

  function test_span_runs_either_way_up() {
    var down = Logic.dragSpan(540, 630, wholeDay)
    compare(down.start, 540)
    compare(down.end, 630)
    var up = Logic.dragSpan(630, 540, wholeDay)
    compare(up.start, 540)
    compare(up.end, 630)
  }

  function test_span_is_at_least_a_quarter_hour() {
    var still = Logic.dragSpan(540, 540, wholeDay)
    compare(still.start, 540)
    compare(still.end, 555)
    // At the foot of the rail it grows upwards instead.
    var foot = Logic.dragSpan(24 * 60, 24 * 60, wholeDay)
    compare(foot.start, 24 * 60 - 15)
    compare(foot.end, 24 * 60)
  }

  function test_new_event_over_a_span() {
    var event = Logic.newEventAt("2026-10-01", 9 * 60 + 15, 10 * 60 + 45, "cal")
    compare(event.start, "2026-10-01T09:15:00")
    compare(event.end, "2026-10-01T10:45:00")
    compare(event.calendarUrl, "cal")
    compare(event.uid, "")
    compare(event.allDay, false)
  }

  function test_new_event_ending_at_midnight_ends_next_day() {
    var event = Logic.newEventAt("2026-10-01", 23 * 60, 24 * 60, "cal")
    compare(event.end, "2026-10-02T00:00:00")
    var draft = Logic.editDraft(event, "America/New_York")
    compare(draft.endDate, "2026-10-02")
    compare(draft.endTime, "00:00")
  }

  function test_draft_span_follows_the_draft() {
    var draft = Logic.editDraft(Logic.newEventAt("2026-10-01", 540, 600, "cal"), "")
    var span = Logic.draftSpanOn(draft, "2026-10-01")
    compare(span.start, 540)
    compare(span.end, 600)
    compare(Logic.draftSpanOn(draft, "2026-10-02"), null)

    // Moving the start keeps the length, as the form does.
    var moved = Logic.withField(draft, "startTime", "11:00")
    span = Logic.draftSpanOn(moved, "2026-10-01")
    compare(span.start, 660)
    compare(span.end, 720)

    var late = Logic.editDraft(Logic.newEventAt("2026-10-01", 23 * 60, 24 * 60, "cal"), "")
    compare(Logic.draftSpanOn(late, "2026-10-01").end, 24 * 60)

    compare(Logic.draftSpanOn(Logic.withField(draft, "allDay", true), "2026-10-01"), null)
  }

  function test_span_label() {
    compare(Logic.spanLabel(540, 630, "24h"), "09:00 – 10:30")
    compare(Logic.spanLabel(23 * 60, 24 * 60, "24h"), "23:00 – 00:00")
  }
}

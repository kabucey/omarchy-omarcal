import QtQuick
import QtTest
import "../Logic.js" as Logic

// The day grid's arithmetic. The lanes come from layoutTimed, which the month
// already uses; this is where a block lands on the rail once it has one.
TestCase {
  name: "dayview"

  function timed(title, from, to) {
    return { title: title, allDay: false,
             start: "2026-09-20T" + from + ":00-05:00",
             end: "2026-09-20T" + to + ":00-05:00" }
  }

  function test_hour_labels_are_one_width() {
    var twelve = Logic.hourLabels("12h")
    compare(twelve.length, 24)
    compare(twelve[0], "12 AM")
    compare(twelve[1], "01 AM")
    compare(twelve[12], "12 PM")
    compare(twelve[13], "01 PM")
    compare(twelve[23], "11 PM")
    for (var i = 0; i < 24; i++) compare(twelve[i].length, twelve[0].length)
  }

  function test_hour_labels_24h() {
    var day = Logic.hourLabels("24h")
    compare(day[0], "00:00")
    compare(day[9], "09:00")
    compare(day[23], "23:00")
  }

  // A day opens an hour before its first event so the block is not jammed
  // against the top edge.
  function test_opening_hour() {
    var blocks = Logic.layoutTimed([timed("A", "13:00", "14:00")], "2026-09-20")
    compare(Logic.openingHour(blocks), 12)
  }

  function test_opening_hour_of_an_empty_day() {
    compare(Logic.openingHour([]), 8)
    compare(Logic.openingHour([], 6), 6)
  }

  function test_opening_hour_never_goes_above_midnight() {
    var blocks = Logic.layoutTimed([timed("A", "00:20", "00:50")], "2026-09-20")
    compare(Logic.openingHour(blocks), 0)
  }

  function test_block_geometry() {
    var blocks = Logic.layoutTimed([timed("A", "13:00", "14:30")], "2026-09-20")
    var box = Logic.blockGeometry(blocks[0], 60)
    compare(box.y, 13 * 60)
    compare(box.height, 90)
  }

  // Regression: Scrum of scrums is 9:45–10:00. The former readable-height
  // floor painted it past 10:00, making the timeline tell the wrong duration.
  function test_short_block_keeps_its_true_duration() {
    var blocks = Logic.layoutTimed([timed("Scrum of scrums", "09:45", "10:00")],
                                   "2026-09-20")
    var box = Logic.blockGeometry(blocks[0], 60, 30)
    compare(box.y, 9 * 60 + 45)
    compare(box.height, 15)
  }

  function test_block_geometry_clamps_to_the_day() {
    var box = Logic.blockGeometry({ startMinute: -60, endMinute: 2000 }, 60)
    compare(box.y, 0)
    compare(box.height, 24 * 60)
  }

  function test_block_column_alone() {
    var blocks = Logic.layoutTimed([timed("A", "13:00", "14:00")], "2026-09-20")
    var col = Logic.blockColumn(blocks[0], 300, 4)
    compare(col.x, 0)
    // No neighbour, so no gap is taken out of it.
    compare(col.width, 300)
  }

  function test_block_column_shared() {
    var blocks = Logic.layoutTimed([
      timed("A", "13:00", "15:00"),
      timed("B", "14:00", "16:00")
    ], "2026-09-20")
    compare(blocks[0].lanes, 2)
    var first = Logic.blockColumn(blocks[0], 300, 4)
    var second = Logic.blockColumn(blocks[1], 300, 4)
    compare(first.x, 0)
    compare(first.width, 296)
    compare(second.x, 124)
    compare(second.width, 172)
  }

  // Regression: a crowded afternoon used to divide the rail into six tiny
  // strips. Cascading keeps every card wide enough to carry useful text while
  // the exposed shoulders still reveal that several appointments are present.
  function test_busy_cluster_cascades_without_unreadable_slivers() {
    var blocks = Logic.layoutTimed([
      timed("A", "13:00", "18:00"),
      timed("B", "13:05", "18:00"),
      timed("C", "13:10", "18:00"),
      timed("D", "13:15", "18:00"),
      timed("E", "13:20", "18:00"),
      timed("F", "13:25", "18:00")
    ], "2026-09-20")

    var previousX = -1
    for (var i = 0; i < blocks.length; i++) {
      var col = Logic.blockColumn(blocks[i], 240, 2, 112)
      verify(col.x > previousX)
      verify(col.width >= 112)
      compare(col.x + col.width, 238)
      previousX = col.x
    }
  }

  function test_hour_offset() {
    compare(Logic.hourOffset(0, 40), 0)
    compare(Logic.hourOffset(9, 40), 360)
    compare(Logic.hourOffset(-2, 40), 0)
  }

  // A view holding today opens on the now line, kept a quarter of the way
  // down the rail, so the morning already past is the room above it.
  function test_now_scroll_sits_a_quarter_down() {
    // A whole day at 40px/hour is 960 tall; a 400px viewport shows 10 hours.
    // Now is 12:00, whose line sits at 480.
    compare(Logic.nowScrollY(12 * 60, Logic.WHOLE_DAY, 40, 400, 960), 480 - 100)
  }

  function test_now_scroll_clamps_to_the_edges() {
    // 01:00 stands 40 down: a quarter high is impossible, so the rail opens
    // at the top rather than above its content.
    compare(Logic.nowScrollY(60, Logic.WHOLE_DAY, 40, 400, 960), 0)
    // 23:59 stands near the bottom: the rail opens wherever its content
    // ends, with the line at the foot of the viewport, not below it.
    compare(Logic.nowScrollY(23 * 60 + 59, Logic.WHOLE_DAY, 40, 400, 960),
            960 - 400)
  }

  function test_now_scroll_respects_the_working_window() {
    var win = Logic.dayWindow(7, 19)  // 07:00–19:00, twelve hours
    // 14:30 sits (14:30 − 07:00) = 450 minutes down, so the line is at 300
    // and the rail opens with a quarter (25) above it.
    compare(Logic.nowScrollY(14 * 60 + 30, win, 40, 100, 480), 300 - 25)
    // 06:00 is outside the window: the rail draws no now line at all, so
    // the caller keeps its own opening position.
    compare(Logic.nowScrollY(6 * 60, win, 40, 400, 480), -1)
  }
}

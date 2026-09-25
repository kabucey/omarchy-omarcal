import QtQuick
import QtTest
import "../Logic.js" as Logic

// The week header. The grid itself reuses what the month and the day already
// have: allDayBars for the band, layoutTimed per column.
TestCase {
  name: "weekview"

  function test_heading_within_one_month() {
    var head = Logic.weekHeading(Logic.weekDays("2026-09-09", 0))
    compare(head.title, "September 6 – 12")
    compare(head.meta, "2026")
  }

  function test_heading_across_two_months() {
    var head = Logic.weekHeading(Logic.weekDays("2026-09-02", 0))
    compare(head.title, "August 30 – September 5")
    compare(head.meta, "2026")
  }

  function test_heading_across_a_new_year() {
    var days = Logic.weekDays("2026-12-31", 0)
    var head = Logic.weekHeading(days)
    compare(head.title, "December 27 – January 2")
    // The year belongs in the quieter line, not said twice in the first.
    compare(head.meta, "2026 – 2027")
  }

  function test_heading_follows_the_week_start() {
    var head = Logic.weekHeading(Logic.weekDays("2026-09-09", 1))
    compare(head.title, "September 7 – 13")
  }

  function test_heading_of_nothing() {
    compare(Logic.weekHeading([]).title, "")
    compare(Logic.weekHeading(null).meta, "")
  }

  // The visible column contract: days run left to right and each column owns
  // a weekday/date label rather than the former row label.
  function test_day_column_headers() {
    var days = Logic.weekDays("2026-09-20", 0)
    compare(days.map(Logic.weekDayHeader),
            ["Sun 20", "Mon 21", "Tue 22", "Wed 23",
             "Thu 24", "Fri 25", "Sat 26"])
    compare(Logic.weekDayHeader("not-a-date"), "")
  }
}

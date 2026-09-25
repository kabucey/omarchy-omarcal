import QtQuick
import QtTest
import "../Logic.js" as Logic

// The face shown in the bar follows the stock Omarchy clock's Qt format
// strings, including its ISO-week token.
TestCase {
  name: "Format"

  readonly property string defaultFormat: "dddd HH:mm"

  function test_default_face_matches_stock_omarchy_clock() {
    var when = new Date(2026, 8, 19, 20, 15, 34)
    compare(Qt.formatDateTime(when, defaultFormat), "Saturday 20:15")
  }

  function test_iso_week_token_is_two_digits() {
    var when = new Date(2026, 8, 19, 20, 15, 34)
    var format = "d MMMM 'W'ww yyyy"
    format = format.replace(/ww/g, Logic.isoWeekLiteral("2026-09-19"))
    compare(Qt.formatDateTime(when, format), "19 September W38 2026")
    compare(Logic.isoWeekLiteral("2026-01-01"), "01")
  }
}

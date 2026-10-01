import QtQuick
import QtTest
import "../Logic.js" as Logic

// The calendar list's grouping: accounts head their own calendars, and every
// calendar added by URL shares one Subscriptions group.
TestCase {
  name: "sidebar"

  readonly property var accounts: [
    { id: "icloud:a", user: "a@example.com", provider: "icloud" },
    { id: "webcal:1", user: "Union", provider: "webcal" },
    { id: "google:b", user: "b@example.com", provider: "google" },
    { id: "webcal:2", user: "Chelsea", provider: "webcal" }
  ]
  readonly property var calendars: [
    { url: "1", name: "Family", accountId: "icloud:a" },
    { url: "2", name: "Union", accountId: "webcal:1" },
    { url: "3", name: "Work", accountId: "google:b", enabled: false },
    { url: "4", name: "Holidays", accountId: "google:b" },
    { url: "5", name: "Chelsea", accountId: "webcal:2" }
  ]

  function test_feeds_share_one_group_at_the_foot() {
    var groups = Logic.sidebarGroups(accounts, calendars)
    compare(groups.length, 3)
    compare(groups[0].title, "iCloud")
    compare(groups[0].subtitle, "a@example.com")
    compare(groups[1].title, "Google")
    compare(groups[1].rows.length, 2)
    compare(groups[2].key, "subscriptions")
    compare(groups[2].rows.map(function (r) { return r.calendar.name }).join(","),
            "Union,Chelsea")
    // A feed's row still knows its account, which holds its settings.
    compare(groups[2].rows[1].account.id, "webcal:2")
  }

  function test_no_subscriptions_group_without_feeds() {
    var groups = Logic.sidebarGroups([accounts[0]], calendars)
    compare(groups.length, 1)
    compare(groups[0].kind, "account")
  }

  function test_count_label() {
    var google = Logic.sidebarGroups(accounts, calendars)[1]
    compare(Logic.groupCountLabel(google.rows), "1 of 2 on")
  }

  function test_group_toggle() {
    compare(Logic.withGroupToggled([], "a"), ["a"])
    compare(Logic.withGroupToggled(["a", "b"], "a"), ["b"])
    compare(Logic.withGroupToggled(undefined, "a"), ["a"])
  }
}

import QtQuick
import QtTest
import "../Logic.js" as Logic

// Overlapping events cascade: the later lane sits over the earlier one and
// reveals its shoulder. Hovering the lower card used to raise it, covering
// the card above it so that one disappeared and could not be hovered back.
// These two cards are the delegate's own geometry from the panel (the same
// layoutTimed / blockColumn / blockGeometry arithmetic and a MouseArea per
// card), and the contract is that stacking follows the cascade, never the
// pointer: the top card stays on top and stays reachable either way.
Item {
  id: scene
  width: 240
  height: 200

  function timed(title, from, to) {
    return { title: title, allDay: false,
             start: "2026-09-20T" + from + ":00-05:00",
             end: "2026-09-20T" + to + ":00-05:00" }
  }

  readonly property var blocks: Logic.layoutTimed(
    [timed("Standup", "09:00", "12:00"), timed("Call", "09:30", "10:00")],
    "2026-09-20")

  readonly property real usable: 190
  readonly property real hourHeight: 10

  Repeater {
    model: blocks

    Rectangle {
      id: card
      required property var modelData
      required property int index
      objectName: "card" + index
      x: Logic.blockColumn(modelData, scene.usable, 0).x
      y: Logic.blockGeometry(modelData, scene.hourHeight, 5).y
      width: Logic.blockColumn(modelData, scene.usable, 0).width
      height: Logic.blockGeometry(modelData, scene.hourHeight, 5).height
      color: index === 0 ? "#0000ff" : "#ff0000"
      z: index

      MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
      }
    }
  }

  TestCase {
    name: "hover"
    when: TestCase.Body

    function cardAt(name) {
      for (var i = 0; i < scene.children.length; i++)
        if (scene.children[i].objectName === name) return scene.children[i]
      fail("no card named " + name)
    }

    function areaOf(name) {
      var card = cardAt(name)
      for (var i = 0; i < card.children.length; i++) {
        var child = card.children[i]
        if (child.containsMouse !== undefined) return child
      }
      fail("no mouse area in " + name)
    }

    // The shared point: inside both cards, where the top one wins the stack.
    function overlapPoint() {
      var top = cardAt("card1"), bottom = cardAt("card0")
      return {
        x: Math.round((top.x + top.width / 2)),
        y: Math.round((top.y + top.height / 2))
      }
    }

    // The part only the lower card covers, so the pointer hits it alone.
    function shoulderPoint() {
      var top = cardAt("card1"), bottom = cardAt("card0")
      return {
        x: Math.round(bottom.x + top.x / 2),
        y: Math.round(bottom.y + bottom.height / 2)
      }
    }

    function test_cascade_order_survives_hovering_the_lower_card() {
      var low  = areaOf("card0")
      var high = areaOf("card1")
      var bottom = cardAt("card0")
      var top = cardAt("card1")

      compare(bottom.z, 0)
      compare(top.z, 1)

      // Hover the lower card's exposed shoulder.
      var shoulder = shoulderPoint()
      mouseMove(scene, shoulder.x, shoulder.y)
      compare(low.containsMouse, true)
      // The point must not re-stack the group: the lower card stays below.
      compare(top.z, bottom.z + 1)

      // The card above it is still on top and can be hovered right back,
      // the way it could before the other card was touched.
      var overlap = overlapPoint()
      mouseMove(scene, overlap.x, overlap.y)
      compare(high.containsMouse, true)
      compare(low.containsMouse, false)
      compare(top.z, bottom.z + 1)
    }

    function test_rehover_cycling_leaves_the_group_reachable() {
      var low  = areaOf("card0")
      var high = areaOf("card1")
      var bottom = cardAt("card0")
      var top = cardAt("card1")
      var shoulder = shoulderPoint()
      var overlap = overlapPoint()
      var away = { x: 230, y: 190 }

      // The reported workaround: off and on a few times. After it, the
      // group works the way it started, from either point.
      for (var i = 0; i < 4; i++) {
        mouseMove(scene, away.x, away.y)
        mouseMove(scene, shoulder.x, shoulder.y)
        mouseMove(scene, overlap.x, overlap.y)
      }
      compare(high.containsMouse, true)
      compare(low.containsMouse, false)
      compare(bottom.z, 0)
      compare(top.z, 1)

      // ...and the shoulder still reaches the lower card.
      mouseMove(scene, away.x, away.y)
      mouseMove(scene, shoulder.x, shoulder.y)
      compare(low.containsMouse, true)
      compare(high.containsMouse, false)
    }
  }
}

// All page behaviour lives in this file. The content security policy allows
// no inline scripts or handlers, so templates mark elements with data-
// attributes and this file acts on them:
//   data-opens            a row or card that loads its panel on click or Enter
//   data-action="..."     a button that does something in the page (see ACTIONS)
//   data-after="..."      what to do once the element's own request succeeds
(function () {
  "use strict";

  function overlay() {
    return document.querySelector(".modal-overlay");
  }

  // --- slide-over panels ------------------------------------------------

  function closePanel() {
    var open = overlay();
    if (open) open.remove();
    document.dispatchEvent(new Event("panelClosed"));
  }

  // Outline the tracker card the slide-over shows, including after the
  // board redraws.
  function markOpenCard() {
    var open = document.querySelector(".modal[data-app]");
    document.querySelectorAll(".app-card.sel").forEach(function (card) {
      card.classList.remove("sel");
    });
    var card = open && document.getElementById("app-" + open.dataset.app);
    if (card) card.classList.add("sel");
  }
  document.addEventListener("htmx:afterSettle", markOpenCard);
  document.addEventListener("panelClosed", markOpenCard);

  document.addEventListener("keydown", function (evt) {
    if (evt.key === "Escape" && overlay()) closePanel();
  });

  // Clicking off a slide-over closes it. Clicking another card or row
  // leaves it to swap in that item's panel instead.
  document.addEventListener("click", function (evt) {
    if (!overlay() || !document.contains(evt.target)) return;
    if (evt.target.closest(".modal, .app-card, .contact-row")) return;
    closePanel();
  });

  // Keyboard users land inside a panel when it opens.
  document.addEventListener("htmx:afterSettle", function (evt) {
    if (evt.detail.target.id !== "modal") return;
    var panel = evt.detail.target.querySelector(".modal");
    if (panel) {
      panel.tabIndex = -1;
      panel.focus({ preventScroll: true });
    }
  });

  // --- rows and cards that open a panel ---------------------------------

  // A click or Enter on the row opens it, except on the row's own controls.
  function openFrom(evt) {
    var row = evt.target.closest("[data-opens]");
    if (!row || evt.target.closest("a, button")) return;
    htmx.trigger(row, "openPanel");
  }
  document.addEventListener("click", openFrom);
  document.addEventListener("keyup", function (evt) {
    if (evt.key === "Enter") openFrom(evt);
  });

  // --- matches: the report beside the list ------------------------------

  // The report says which job it shows; the list highlights that row.
  document.addEventListener("htmx:afterSwap", function (evt) {
    if (evt.detail.target.id !== "report") return;
    evt.detail.target.scrollTop = 0;
    var shown = evt.detail.target.querySelector("[data-job]");
    document.querySelectorAll(".job-row.sel").forEach(function (row) {
      row.classList.remove("sel");
    });
    var row = shown && document.getElementById("job-" + shown.dataset.job);
    if (row) row.classList.add("sel");
    // Narrow screens stack the report under the list, so take the click to it.
    var cause = evt.detail.requestConfig && evt.detail.requestConfig.triggeringEvent;
    if (cause && window.innerWidth <= 1050) evt.detail.target.scrollIntoView();
  });

  // Dismissing drops the row and moves the report on to the next job.
  function dropRow(jobId) {
    var row = document.getElementById("job-" + jobId);
    if (!row) return;
    var next = row.nextElementSibling;
    var wasShown = row.classList.contains("sel");
    row.remove();
    if (!wasShown) return;
    if (next && next.classList.contains("job-row")) next.click();
    else document.getElementById("report").innerHTML = "";
  }

  // Tracking from the row refreshes the report if it shows that job.
  document.addEventListener("jobTracked", function (evt) {
    var jobId = Number(evt.detail.value);
    if (document.querySelector('#report [data-job="' + jobId + '"]')) {
      htmx.ajax("GET", "/jobs/" + jobId + "/detail", "#report");
    }
  });

  // --- data-action: buttons that act in the page -------------------------

  var ACTIONS = {
    "close-panel": closePanel,
    // Opens or closes the <details> named by data-target.
    "toggle": function (button) {
      var details = document.getElementById(button.dataset.target);
      if (details) details.open = !details.open;
    },
    // Copies the text of the element just before the button.
    "copy-previous": function (button) {
      navigator.clipboard.writeText(button.previousElementSibling.textContent);
      button.textContent = "Copied";
    },
    // The report's Track button presses the one on that job's row.
    "track-row": function (button) {
      var track = document.querySelector("#job-" + Number(button.dataset.job) + " .track");
      if (track) track.click();
    }
  };
  document.addEventListener("click", function (evt) {
    var button = evt.target.closest("[data-action]");
    var action = button && ACTIONS[button.dataset.action];
    if (action) action(button);
  });

  // --- data-after: follow-ups once an element's request succeeds ---------

  var AFTER = {
    "close-panel": closePanel,
    "drop-row": function (elt) {
      dropRow(Number(elt.dataset.job));
    },
    // The add-contact form clears only when the contact was really saved.
    "reset-if-saved": function (form, xhr) {
      if (xhr.getResponseHeader("HX-Trigger")) form.reset();
    }
  };
  document.addEventListener("htmx:afterRequest", function (evt) {
    var elt = evt.detail.elt;
    var after = evt.detail.successful && elt && elt.dataset && AFTER[elt.dataset.after];
    if (after) after(elt, evt.detail.xhr);
  });

  // --- tracker: keep the board where it was across redraws ----------------

  // Replacing all of #board (lane select, suggestion accept/dismiss,
  // untrack) destroys the browser's scroll anchor and the page jumps to
  // the bottom, so pin the scroll position across those swaps.
  var boardScroll = null;
  var openFolds = [];
  function rememberBoardScroll(evt) {
    if (evt.detail.target && evt.detail.target.id === "board") {
      boardScroll = window.scrollY;
      // Show N more stays open across the redraw too.
      openFolds = Array.from(document.querySelectorAll(".lane-more[open]")).map(function (fold) {
        return fold.dataset.lane;
      });
    }
  }
  document.addEventListener("htmx:beforeSwap", rememberBoardScroll);
  document.addEventListener("htmx:oobBeforeSwap", rememberBoardScroll);
  document.addEventListener("htmx:afterSettle", function () {
    if (boardScroll === null) return;
    document.querySelectorAll(".lane-more").forEach(function (fold) {
      if (openFolds.indexOf(fold.dataset.lane) !== -1) fold.open = true;
    });
    window.scrollTo(0, boardScroll);
    boardScroll = null;
  });

  // --- refused or failed requests say so ----------------------------------

  // A request the server turns away (too long, read-only demo, not found)
  // would otherwise fail without a word, because HTMX swaps nothing in.
  function reason(xhr) {
    var kind = xhr.getResponseHeader("Content-Type") || "";
    if (kind.indexOf("text/plain") === 0) return xhr.responseText;
    if (kind.indexOf("application/json") === 0) {
      try {
        var detail = JSON.parse(xhr.responseText).detail;
        if (typeof detail === "string") return detail;
      } catch (err) { /* fall through to the generic line */ }
    }
    return "That didn't work (error " + xhr.status + ").";
  }
  document.addEventListener("htmx:responseError", function (evt) {
    var toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = reason(evt.detail.xhr);
    toast.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () { toast.hidden = true; }, 5000);
  });
})();

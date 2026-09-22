/* Email marketing: interface behaviour.
   No framework on purpose: the whole tool is a few hundred lines and a build
   step would be more machinery than the thing it builds. */
(function () {
  "use strict";

  // --- toasts ---------------------------------------------------------------
  function toast(text, kind) {
    var box = document.getElementById("toasts");
    if (!box) { box = document.createElement("div"); box.id = "toasts"; document.body.appendChild(box); }
    var el = document.createElement("div");
    el.className = "toast" + (kind === "warn" ? " warn" : "");
    el.textContent = text;
    box.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .3s"; el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 320);
    }, 4200);
  }
  window.toast = toast;

  // --- confirm, as a real dialog -------------------------------------------
  // The browser's confirm() cannot say how many people are about to be emailed
  // in anything but one grey line, and that is the number that matters most.
  function ask(opts) {
    return new Promise(function (resolve) {
      var back = document.createElement("div");
      back.className = "modal-back";
      back.innerHTML =
        '<div class="modal" role="dialog" aria-modal="true">' +
        '<h3></h3><p></p>' +
        '<div class="actions">' +
        '<button class="btn" data-no>Cancel</button>' +
        '<button class="btn primary" data-yes></button>' +
        '</div></div>';
      back.querySelector("h3").textContent = opts.title || "Are you sure?";
      back.querySelector("p").textContent = opts.body || "";
      back.querySelector("[data-yes]").textContent = opts.ok || "Continue";
      document.body.appendChild(back);
      function done(v) { back.remove(); resolve(v); }
      back.querySelector("[data-yes]").onclick = function () { done(true); };
      back.querySelector("[data-no]").onclick = function () { done(false); };
      back.onclick = function (e) { if (e.target === back) done(false); };
      document.addEventListener("keydown", function esc(e) {
        if (e.key === "Escape") { document.removeEventListener("keydown", esc); done(false); }
      });
      back.querySelector("[data-yes]").focus();
    });
  }

  // Any form carrying data-confirm gets the dialog instead of submitting straight.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.dataset || !form.dataset.confirm || form.dataset.ok === "1") return;
    e.preventDefault();
    ask({ title: form.dataset.confirmTitle || "Are you sure?",
          body: form.dataset.confirm,
          ok: form.dataset.confirmOk || "Continue" }).then(function (yes) {
      if (yes) { form.dataset.ok = "1"; form.submit(); }
    });
  });

  // --- composer: live preview and live audience count -----------------------
  var composer = document.getElementById("composer");
  if (composer) {
    var frame = document.getElementById("preview");
    var body = composer.querySelector("[name=body]");
    // Layout campaigns send their field values and let the server render them,
    // so the preview goes through the same code as a real send. A second
    // renderer here in JS would eventually disagree with it, and the preview
    // being wrong is worse than no preview.
    var layoutInput = composer.querySelector("[name=layout]");
    var fieldInputs = composer.querySelectorAll("[data-field]");
    var subject = composer.querySelector("[name=subject]");
    var preheader = composer.querySelector("[name=preheader]");
    var audience = composer.querySelector("[name=audience]");
    var reachOut = document.getElementById("reach");
    var subjOut = document.getElementById("subjline");
    var timer = null;

    // Reading the preview in English is a way to check the Dutch, never a way
    // to send English. It only ever affects this pane.
    var showEnglish = false;

    function values() {
      var out = {};
      fieldInputs.forEach(function (el) { out[el.dataset.field] = el.value; });
      return out;
    }

    function paint() {
      if (!frame) return;
      fetch("/api/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          body: body ? body.value : "",
          layout: layoutInput ? layoutInput.value : "",
          values: values(),
          english: showEnglish,
          // The subject is shown above the pane, so it has to be translated
          // with the rest of it. Left out, pressing English translated the
          // whole email and left its first line in Dutch.
          subject: subject ? subject.value : "",
          // So the footer shows the line THIS audience will really receive.
          // Without it every preview showed the newsletter wording, including
          // for campaigns aimed at people who have never subscribed to one.
          audience: audience ? audience.value : "",
          preheader: preheader ? preheader.value : ""
        })
      }).then(function (r) {
        // The subject sits above the pane, so it is translated with the rest of
        // it. Without this, pressing English translated the whole email and
        // left its first line in Dutch.
        var line = r.headers.get("X-Preview-Subject");
        if (subjOut && line !== null) {
          try { subjOut.textContent = decodeURIComponent(line) || "(no subject)"; }
          catch (e) { /* leave whatever is there */ }
        }
        return r.text();
      }).then(function (html) {
        // srcdoc rather than a URL: the preview is never a real page, so it
        // cannot be linked to, cached, or accidentally indexed.
        frame.srcdoc = html;
      }).catch(function () { /* preview is a nicety, never an error */ });
    }
    function schedule() { clearTimeout(timer); timer = setTimeout(paint, 350); }

    if (body) body.addEventListener("input", schedule);
    fieldInputs.forEach(function (el) { el.addEventListener("input", schedule); });
    if (preheader) preheader.addEventListener("input", schedule);
    if (subject && subjOut) {
      // Sync once on load as well as on every keystroke: on a new campaign the
      // box arrives already filled in from the layout, and a label that
      // disagrees with the field beside it reads as a bug in the saving.
      function syncSubject() {
        subjOut.textContent = subject.value || "(no subject)";
      }
      subject.addEventListener("input", syncSubject);
      syncSubject();
    }
    if (audience && reachOut) {
      audience.addEventListener("change", function () {
        schedule();          // the footer line depends on the audience now
        reachOut.textContent = "…";
        fetch("/api/audience/" + encodeURIComponent(audience.value))
          .then(function (r) { return r.json(); })
          .then(function (d) { reachOut.textContent = d.count; })
          .catch(function () { reachOut.textContent = "?"; });
      });
    }
    paint();

    // phone / desktop preview
    document.querySelectorAll("[data-w]").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll("[data-w]").forEach(function (x) { x.classList.remove("on"); });
        b.classList.add("on");
        frame.classList.toggle("phone", b.dataset.w === "phone");
      });
    });

    // dutch / english preview
    document.querySelectorAll("[data-lang]").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll("[data-lang]").forEach(function (x) { x.classList.remove("on"); });
        b.classList.add("on");
        showEnglish = b.dataset.lang === "en";
        paint();
      });
    });

    // Ctrl/Cmd+S saves, because people type into a textarea and expect it to.
    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); composer.submit(); }
    });
  }

  // --- sending: one button, runs the whole list -----------------------------
  // Before this, the send button did 25 and you pressed it again. Twelve times for
  // 300 people, with no way to see where it had got to.
  var sendBox = document.getElementById("sendbox");
  if (sendBox) {
    var cid = sendBox.dataset.campaign;
    var startBtn = document.getElementById("start-send");
    var stopBtn = document.getElementById("stop-send");
    var bar = document.querySelector("#sendbox .bar i");
    var live = document.getElementById("sendlive");
    var stats = { sent: 0, total: parseInt(sendBox.dataset.total || "0", 10) };
    var running = false, stopping = false;

    function show(txt, spin) {
      live.innerHTML = (spin ? '<span class="spinner"></span>' : "") +
                       "<span>" + txt + "</span>";
    }
    function draw() {
      if (!stats.total) return;
      bar.style.width = Math.round((stats.sent / stats.total) * 100) + "%";
    }

    function step() {
      if (stopping) { running = false; show("Paused at " + stats.sent + " of " + stats.total, false);
                      startBtn.disabled = false; stopBtn.hidden = true; return; }
      fetch("/api/campaign/" + cid + "/send", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.error) { show(d.error, false); toast(d.error, "warn"); running = false;
                         startBtn.disabled = false; stopBtn.hidden = true; return; }
          stats.sent = d.sent_total; stats.total = d.queued;
          draw();
          if (d.done) {
            running = false; stopBtn.hidden = true;
            show("Done. " + d.sent_total + " sent" +
                 (d.failed ? ", " + d.failed + " failed" : "") + ".", false);
            toast("Campaign sent to " + d.sent_total + " people.");
            setTimeout(function () { location.reload(); }, 1400);
            return;
          }
          if (d.stopped) {
            running = false; startBtn.disabled = false; stopBtn.hidden = true;
            show("Stopped: " + d.stopped, false);
            toast("Stopped: " + d.stopped, "warn");
            return;
          }
          show("Sending… " + d.sent_total + " of " + d.queued, true);
          setTimeout(step, 250);
        })
        .catch(function () {
          running = false; startBtn.disabled = false; stopBtn.hidden = true;
          show("Lost the connection. Press again to continue.", false);
        });
    }

    if (startBtn) startBtn.addEventListener("click", function () {
      if (running) return;
      ask({
        title: "Send campaign",
        body: "This sends real email to " + (stats.total || sendBox.dataset.reach) +
              " people. It cannot be undone.",
        ok: "Yes, send"
      }).then(function (yes) {
        if (!yes) return;
        running = true; stopping = false;
        startBtn.disabled = true; stopBtn.hidden = false;
        show("Sending…", true);
        step();
      });
    });

    if (stopBtn) stopBtn.addEventListener("click", function () {
      stopping = true; show("Stopping after this block…", true);
    });

    draw();
  }

  // --- subscribers: filter as you type --------------------------------------
  var q = document.getElementById("filter");
  if (q) {
    var rows = Array.prototype.slice.call(document.querySelectorAll("#sublist tbody tr[data-search]"));
    var countOut = document.getElementById("shown");
    q.addEventListener("input", function () {
      var needle = q.value.trim().toLowerCase();
      var n = 0;
      rows.forEach(function (tr) {
        var hit = !needle || tr.dataset.search.indexOf(needle) !== -1;
        tr.hidden = !hit;
        if (hit) n++;
      });
      if (countOut) countOut.textContent = n;
    });
  }


  // --- composer toolbar: insert HTML so nobody has to type it ---------------
  (function () {
    var area = document.getElementById("f-body");
    if (!area) return;
    var snippets = {};
    document.querySelectorAll("[data-snippet]").forEach(function (el) {
      snippets[el.dataset.snippet] = el.textContent;
    });
    document.querySelectorAll("[data-insert]").forEach(function (b) {
      b.addEventListener("click", function () {
        var html = snippets[b.dataset.insert];
        if (!html) return;
        // At the cursor, not at the end: people write in the middle of a draft.
        var start = area.selectionStart, end = area.selectionEnd;
        var before = area.value.slice(0, start), after = area.value.slice(end);
        var pad = (before && !before.endsWith("\n")) ? "\n" : "";
        area.value = before + pad + html + "\n" + after;
        var caret = (before + pad + html).length;
        area.focus();
        area.setSelectionRange(caret, caret);
        area.dispatchEvent(new Event("input", { bubbles: true }));
      });
    });
  })();


  // --- subscriber row actions, without leaving the page ---------------------
  // These used to be little forms that POSTed and redirected. On a list of 100
  // that meant every correction threw away your scroll position and your filter,
  // which made fixing three rows in a row genuinely unpleasant.
  (function () {
    var table = document.getElementById("sublist");
    if (!table) return;

    function paintStats(stats) {
      if (!stats) return;
      var map = { mailable: "s-mailable", unsubscribed: "s-unsub",
                  never: "s-never", total: "s-total" };
      Object.keys(map).forEach(function (k) {
        var el = document.getElementById(map[k]);
        if (el && stats[k] !== undefined) el.textContent = stats[k];
      });
    }

    table.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      e.preventDefault();

      ask({ title: btn.dataset.title, body: btn.dataset.ask, ok: btn.dataset.ok })
        .then(function (yes) {
          if (!yes) return;
          var row = btn.closest("tr");
          row.classList.add("busy");
          btn.disabled = true;

          var body = new URLSearchParams();
          if (btn.dataset.extra) {
            btn.dataset.extra.split("&").forEach(function (pair) {
              var bits = pair.split("=");
              body.append(bits[0], bits[1]);
            });
          }
          fetch("/api/subscriber/" + btn.dataset.id + "/" + btn.dataset.act,
                { method: "POST", body: body })
            .then(function (r) { return r.json().then(function (d) { return [r.ok, d]; }); })
            .then(function (pair) {
              var okResp = pair[0], d = pair[1];
              if (!okResp || !d.ok) {
                row.classList.remove("busy");
                btn.disabled = false;
                toast(d.message || "That did not work.", "warn");
                return;
              }
              // Swap in the row the SERVER rendered, so the screen can never
              // show a state the database does not hold.
              var tmp = document.createElement("tbody");
              tmp.innerHTML = d.row.trim();
              var fresh = tmp.firstElementChild;
              row.replaceWith(fresh);
              fresh.classList.add("flash-ok");
              setTimeout(function () { fresh.classList.remove("flash-ok"); }, 1200);
              paintStats(d.stats);
              toast(d.message);
            })
            .catch(function () {
              row.classList.remove("busy");
              btn.disabled = false;
              toast("Lost the connection. Nothing was changed.", "warn");
            });
        });
    });
  })();

  // --- bulk consent changes -------------------------------------------------
  // Only ever takes people OUT of the mailing list. There is no bulk way to put
  // anybody in, on purpose: consent is given one person at a time.
  (function () {
    var bar = document.getElementById("bulkbar");
    var form = document.getElementById("bulkform");
    if (!bar || !form) return;

    var list = document.getElementById("sublist");
    var pickAll = document.getElementById("pickall");
    var countOut = document.getElementById("bulkcount");
    var matchingLink = document.getElementById("pickmatching");
    var pickedAll = document.getElementById("pickedall");
    var total = parseInt(matchingLink ? matchingLink.dataset.total || "0" : "0", 10);
    var allMatching = false;

    function boxes() { return Array.prototype.slice.call(list.querySelectorAll(".rowpick")); }
    function picked() { return boxes().filter(function (b) { return b.checked; }); }

    function refresh() {
      var all = boxes(), on = picked();
      countOut.textContent = allMatching ? total : on.length;
      bar.hidden = on.length === 0;
      pickAll.checked = all.length > 0 && on.length === all.length;
      pickAll.indeterminate = on.length > 0 && on.length < all.length;
      // Offer the whole filter only once this page is fully ticked and there is
      // genuinely more beyond it.
      var offer = !allMatching && all.length > 0 &&
                  on.length === all.length && total > all.length;
      if (matchingLink) matchingLink.hidden = !offer;
      if (pickedAll) pickedAll.hidden = !allMatching;
      if (allMatching) countOut.parentNode.hidden = true;
      else countOut.parentNode.hidden = false;
    }

    list.addEventListener("change", function (e) {
      if (!e.target.classList.contains("rowpick")) return;
      // Un-ticking anything drops out of "the whole filter": the selection on
      // screen no longer matches the claim.
      if (!e.target.checked) allMatching = false;
      refresh();
    });

    pickAll.addEventListener("change", function () {
      allMatching = false;
      boxes().forEach(function (b) { b.checked = pickAll.checked; });
      refresh();
    });

    if (matchingLink) {
      matchingLink.addEventListener("click", function (e) {
        e.preventDefault();
        allMatching = true;
        refresh();
      });
    }

    document.getElementById("bulkclear").addEventListener("click", function () {
      allMatching = false;
      boxes().forEach(function (b) { b.checked = false; });
      refresh();
    });

    document.querySelectorAll("[data-bulk]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var ids = picked().map(function (b) { return b.value; });
        var n = allMatching ? total : ids.length;
        if (!n) return;
        var kind = btn.dataset.bulk;
        var dialog;
        if (kind === "grant") {
          // Word for word the question the single-row button asks, because it
          // is the same claim, just made about more people at once.
          dialog = {
            title: "Mark " + n + " people as subscribed?",
            body: "Only do this if all " + n + " actually gave permission, at " +
                  "the counter, on paper, or by email. Confirming states that " +
                  "they did. Anyone who has unsubscribed is skipped.",
            ok: "Yes, they opted in"
          };
        } else if (kind === "restore") {
          dialog = {
            title: "Put " + n + " people back on the list?",
            body: "This only reverses unsubscribes made from this screen, which " +
                  "corrects the record. Anyone who clicked the unsubscribe link " +
                  "in their own email is left alone. Those can only be put back " +
                  "one at a time, and only if they asked you to.",
            ok: "Put " + n + " back"
          };
        } else if (kind === "unsubscribe") {
          dialog = {
            title: "Unsubscribe " + n + " people?",
            body: "They will not receive anything again unless they ask to " +
                  "rejoin. This was done from this screen, so it can be undone " +
                  "row by row.",
            ok: "Unsubscribe " + n
          };
        } else {
          dialog = {
            title: "Remove consent from " + n + " people?",
            body: "Their addresses are kept, but nothing will be sent to them.",
            ok: "Remove consent from " + n
          };
        }
        ask(dialog).then(function (yes) {
          if (!yes) return;
          form.querySelector("[name=action]").value = kind;
          form.querySelector("[name=they_opted_in]").value =
            kind === "grant" ? "yes" : "";
          form.querySelector("[name=all_matching]").value = allMatching ? "1" : "";
          if (!allMatching) {
            ids.forEach(function (id) {
              var i = document.createElement("input");
              i.type = "hidden"; i.name = "ids"; i.value = id;
              form.appendChild(i);
            });
          }
          form.submit();
        });
      });
    });

    refresh();
  })();

  // --- columns: show, hide, resize ------------------------------------------
  // Which columns somebody wants and how wide they want them is a preference,
  // not company policy, so it is kept in this browser and never on the server.
  // localStorage can throw outright in a private window, so every touch of it
  // is wrapped: a lost preference is a shrug, a page that will not load is not.
  (function () {
    var table = document.querySelector("#sublist table");
    if (!table) return;
    var KEY = "ela.subscribers.columns";

    function load() {
      try { return JSON.parse(localStorage.getItem(KEY)) || {}; }
      catch (e) { return {}; }
    }
    function save(state) {
      try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
    }
    var state = load();
    state.hidden = state.hidden || [];
    state.widths = state.widths || {};

    function cells(col) {
      return table.querySelectorAll('[data-col="' + col + '"]');
    }
    function apply() {
      table.querySelectorAll("[data-col]").forEach(function (el) {
        el.hidden = state.hidden.indexOf(el.dataset.col) !== -1;
      });
      // Explicit widths only hold under a fixed layout; without one the browser
      // treats them as suggestions and a drag does nothing. The class also
      // turns on clipping, because a fixed layout does NOT make text respect
      // its cell: long addresses simply draw straight over the next column.
      var any = Object.keys(state.widths).length > 0;
      table.classList.toggle("resized", any);
      Object.keys(state.widths).forEach(function (col) {
        cells(col).forEach(function (el) { el.style.width = state.widths[col] + "px"; });
      });
      document.querySelectorAll("[data-toggle]").forEach(function (box) {
        box.checked = state.hidden.indexOf(box.dataset.toggle) === -1;
      });
    }

    function snapshotWidths() {
      // Freeze what the browser has already worked out for EVERY column before
      // switching to a fixed layout. Setting one width and letting the rest be
      // redistributed is what makes the whole table jump on the first drag.
      table.querySelectorAll("tr.cols th").forEach(function (th) {
        var col = th.dataset.col;
        if (col && !state.widths[col]) state.widths[col] = th.offsetWidth;
      });
    }

    document.querySelectorAll("[data-toggle]").forEach(function (box) {
      box.addEventListener("change", function () {
        var col = box.dataset.toggle;
        var at = state.hidden.indexOf(col);
        if (box.checked && at !== -1) state.hidden.splice(at, 1);
        else if (!box.checked && at === -1) state.hidden.push(col);
        save(state); apply();
      });
    });

    var reset = document.getElementById("colreset");
    if (reset) {
      reset.addEventListener("click", function () {
        table.querySelectorAll("[data-col]").forEach(function (el) {
          el.style.width = "";
        });
        state = { hidden: [], widths: {} };
        save(state); apply();
        toast("Columns reset.");
      });
    }

    // The little popover.
    var btn = document.getElementById("colbtn");
    var pop = document.getElementById("colpop");
    if (btn && pop) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        pop.hidden = !pop.hidden;
        btn.setAttribute("aria-expanded", String(!pop.hidden));
      });
      document.addEventListener("click", function (e) {
        if (!pop.hidden && !pop.contains(e.target)) {
          pop.hidden = true; btn.setAttribute("aria-expanded", "false");
        }
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && !pop.hidden) {
          pop.hidden = true; btn.setAttribute("aria-expanded", "false");
        }
      });
    }

    // Drag the right edge of a heading to resize that column.
    var dragging = null;
    table.querySelectorAll(".grip").forEach(function (grip) {
      grip.addEventListener("mousedown", function (e) {
        var th = grip.closest("th");
        snapshotWidths();          // before anything moves
        apply();
        dragging = { col: th.dataset.col, x: e.clientX, w: th.offsetWidth };
        document.body.classList.add("resizing");
        e.preventDefault();      // or the browser starts a text selection
      });
    });
    document.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      // A floor, because a column dragged to nothing is unrecoverable without
      // knowing the reset button exists.
      var w = Math.max(60, dragging.w + (e.clientX - dragging.x));
      state.widths[dragging.col] = w;
      cells(dragging.col).forEach(function (el) { el.style.width = w + "px"; });
    });
    document.addEventListener("mouseup", function () {
      if (!dragging) return;
      dragging = null;
      document.body.classList.remove("resizing");
      save(state);
    });

    apply();
  })();

  // --- paging from the keyboard ---------------------------------------------
  // 5.778 people is 116 pages. Reaching the bottom of a page and having to go
  // back to the mouse every fifty rows is the whole cost of a long list.
  (function () {
    var pager = document.querySelector(".pager");
    if (!pager) return;

    document.addEventListener("keydown", function (e) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      // Never steal a key from somebody typing, including in the page box.
      var el = document.activeElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" ||
                 el.tagName === "SELECT" || el.isContentEditable)) return;
      var want = e.key === "ArrowLeft" ? "Previous page"
               : e.key === "ArrowRight" ? "Next page" : null;
      if (!want) return;
      var link = pager.querySelector('a.pgbtn[aria-label="' + want + '"]');
      if (!link) return;          // already at that end
      e.preventDefault();
      window.location = link.href;
    });

    // Typing a page number and pressing enter submits, which the form does on
    // its own. This only stops a value outside the range going to the server.
    var jump = pager.querySelector(".jump");
    if (jump) {
      jump.addEventListener("submit", function (e) {
        var box = jump.querySelector("[name=page]");
        var pages = parseInt(pager.dataset.pages || "1", 10);
        var n = parseInt(box.value, 10);
        if (!n || n < 1 || n > pages) {
          e.preventDefault();
          box.value = pager.dataset.page;
          toast("There " + (pages === 1 ? "is" : "are") + " only " + pages +
                " page" + (pages === 1 ? "" : "s") + ".", "warn");
        }
      });
    }
  })();

  // Flash message handed over from the server becomes a toast.
  var flash = document.getElementById("flash");
  if (flash && flash.textContent.trim()) {
    toast(flash.textContent.trim(), flash.dataset.kind || "");
    flash.remove();
  }

  // --- reading a flow email in place ----------------------------------------
  // Each step opens the real rendered email next to the sequence, in Dutch or
  // English. It is a drawer rather than a new tab because the question is
  // almost always "is the second one different enough from the first", and
  // three browser tabs cannot answer that.
  var mv = document.getElementById("mv");
  if (mv) {
    var mvBack = document.getElementById("mv-back");
    var mvFrame = document.getElementById("mv-frame");
    var mvTitle = document.getElementById("mv-title");
    var mvSub = document.getElementById("mv-sub");
    var mvLang = document.getElementById("mv-lang");
    var current = null;

    function load(lang) {
      if (!current) return;
      mvFrame.src = current + (lang === "en" ? "?english=1" : "");
      mvLang.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("on", b.dataset.lang === lang);
      });
    }

    function openCard(card) {
      current = card.dataset.preview;
      mvTitle.textContent = card.dataset.title || "Email";
      mvSub.textContent = card.dataset.sub || "";
      document.querySelectorAll(".seq-mail.on").forEach(function (el) {
        el.classList.remove("on");
      });
      card.classList.add("on");
      load("nl");
      mv.hidden = false;
      mvBack.hidden = false;
    }

    function closeDrawer() {
      mv.hidden = true;
      mvBack.hidden = true;
      mvFrame.removeAttribute("src");
      document.querySelectorAll(".seq-mail.on").forEach(function (el) {
        el.classList.remove("on");
      });
    }

    document.querySelectorAll("[data-preview]").forEach(function (card) {
      card.addEventListener("click", function () { openCard(card); });
      card.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCard(card); }
      });
    });
    mvLang.querySelectorAll("button").forEach(function (b) {
      b.addEventListener("click", function () { load(b.dataset.lang); });
    });
    document.getElementById("mv-close").addEventListener("click", closeDrawer);
    mvBack.addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !mv.hidden) closeDrawer();
    });
  }

  // --- new flow form --------------------------------------------------------
  // Opening a panel rather than a page: creating a flow is a name and a
  // trigger, and the real work is writing the emails afterwards.
  document.querySelectorAll("[data-panel]").forEach(function (b) {
    b.addEventListener("click", function () {
      var panel = document.getElementById(b.dataset.panel);
      if (!panel) return;
      panel.hidden = !panel.hidden;
      if (!panel.hidden) {
        var first = panel.querySelector("input, select");
        if (first) first.focus();
      }
    });
  });

  // What the chosen trigger actually does, in a line under the dropdown, plus
  // the one field only the win back trigger needs.
  var nfTrigger = document.getElementById("nf-trigger");
  if (nfTrigger) {
    var hints = {};
    try { hints = JSON.parse(document.getElementById("nf-hints").textContent); }
    catch (e) { hints = {}; }
    var nfHint = document.getElementById("nf-hint");
    var nfDays = document.querySelector(".nf-days");
    function describe() {
      var t = hints[nfTrigger.value] || {};
      nfHint.textContent = t.hint || "";
      // Two triggers read the day count and they mean opposite things:
      // "quiet for this long" and "this long after buying". Hidden for the
      // rest, because a number nobody needs is a number somebody will set.
      var usesDays = { winback: "Quiet for how many days",
                       bought: "How many days after the purchase" };
      if (nfDays) {
        nfDays.hidden = !usesDays[nfTrigger.value];
        var label = nfDays.childNodes[0];
        if (label && usesDays[nfTrigger.value]) {
          label.nodeValue = usesDays[nfTrigger.value];
          var box = nfDays.querySelector("input");
          if (box) box.value = nfTrigger.value === "bought" ? "30" : "365";
        }
      }
    }
    nfTrigger.addEventListener("change", describe);
    describe();
  }

  // A link inside a clickable card is a link, not the card.
  document.querySelectorAll("[data-stop]").forEach(function (el) {
    el.addEventListener("click", function (e) { e.stopPropagation(); });
  });

  // Dutch or English on the step editor's preview, same control as the drawer.
  var pvLang = document.getElementById("pv-lang");
  if (pvLang) {
    var pvFrame = document.getElementById("pv-frame");
    pvLang.querySelectorAll("button").forEach(function (b) {
      b.addEventListener("click", function () {
        pvFrame.src = pvFrame.dataset.src + (b.dataset.lang === "en" ? "?english=1" : "");
        pvLang.querySelectorAll("button").forEach(function (o) {
          o.classList.toggle("on", o === b);
        });
      });
    });
  }

})();

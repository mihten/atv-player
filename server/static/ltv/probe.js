// LTVPlayer device probe (phase P2). Settles the plan's UNVERIFIED questions U1-U11 on the TV; every result goes to
// the server journal as `player[ltv]: probe ...` (it joins as the ltv player and never reports a status).
// Plain ES5 on purpose (U1 is one of the questions). Query: fast=1 (short waits, for desktop tests), only=<a,b>
// (sample labels containing a or b), series=0 (no media series), pause=<s> (length of the paused test, default 240).
(function () {
  "use strict";
  var Q = {};
  location.search.replace(/^\?/, "").split("&").forEach(function (kv) {
    var p = kv.split("=");
    if (p[0]) { Q[decodeURIComponent(p[0])] = decodeURIComponent(p[1] || ""); }
  });
  var FAST = Q.fast === "1";
  var v = document.getElementById("v");
  var t0 = Date.now(), lines = [], queue = [], ws = null, wsOpen = false, keysSeen = 0, errors = 0;
  var lib = [], samples = [];

  function $(id) { return document.getElementById(id); }
  function now() { return Date.now(); }
  function play(ms) { return FAST ? Math.round(ms / 6) : ms; }   // playback stretches only; timeouts stay real

  // ---------------------------------------------------------------- log: screen + server journal
  function send(m) {
    if (wsOpen) { try { ws.send(JSON.stringify(m)); } catch (e) { queue.push(m); } } else { queue.push(m); }
  }
  function log(text) {
    var line = ((now() - t0) / 1000).toFixed(1) + " " + text;
    lines.push(line);
    if (lines.length > 30) { lines.shift(); }
    $("log").textContent = lines.join("\n");
    $("log").scrollTop = $("log").scrollHeight;   // newest line visible at the bottom
    if (window.console) { console.log("probe " + text); }
    send({ type: "log", message: ("probe " + text).slice(0, 1450) });
  }
  function step(title, sub) { $("step").textContent = title; $("sub").textContent = sub || ""; }
  function connect() {
    try {
      ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws?role=player&kind=ltv");
    } catch (e) { setTimeout(connect, 2000); return; }
    ws.onopen = function () {
      wsOpen = true;
      while (queue.length) { ws.send(JSON.stringify(queue.shift())); }
    };
    ws.onclose = function () { wsOpen = false; setTimeout(connect, 2000); };
    ws.onmessage = function () { /* state / load for the ltv queue: the probe plays nothing of it */ };
  }
  setInterval(function () { send({ type: "ping", n: now() }); }, 15000);

  window.addEventListener("error", function (e) {
    if (e.target === v) { return; }   // media errors are reported by the sample test
    errors += 1;
    var t = e.target && e.target !== window ? " resource " + (e.target.src || e.target.href || e.target.tagName) : "";
    log("JSERR " + (e.message || "") + " " + (e.filename || "") + ":" + (e.lineno || "") + t);
  }, true);

  // ---------------------------------------------------------------- U2 / U7: environment
  function env() {
    if (window.innerWidth && window.innerWidth !== 1920) {
      document.documentElement.style.zoom = window.innerWidth / 1920;
    }
    log("env ua=" + navigator.userAgent);
    log("env inner=" + window.innerWidth + "x" + window.innerHeight + " screen=" + screen.width + "x" + screen.height +
        " avail=" + screen.availWidth + "x" + screen.availHeight + " dpr=" + window.devicePixelRatio +
        " zoom=" + (document.documentElement.style.zoom || 1) + " from=" + (Q.from || "-") + " href=" + location.href);
    var palm = window.PalmSystem, names = [];
    if (palm) {
      for (var k in palm) { names.push(k); }
      log("U2 PalmSystem yes: " + names.slice(0, 60).join(","));
      try { log("U2 PalmSystem identifier=" + palm.identifier + " launchParams=" + palm.launchParams); } catch (e1) { /* */ }
      try { log("U2 PalmSystem deviceInfo=" + String(palm.deviceInfo).slice(0, 600)); } catch (e2) { /* */ }
    } else {
      log("U2 PalmSystem NO");
    }
    log("U2 webOSSystem=" + (typeof window.webOSSystem) + " webOS=" + (typeof window.webOS) +
        " platformBack=" + !!(palm && palm.platformBack));
    var css = window.CSS && CSS.supports ? CSS.supports.bind(CSS) : function () { return "n/a"; };
    log("css grid=" + css("display", "grid") + " vars=" + css("--a: 1") + " backdrop=" + css("backdrop-filter", "blur(4px)") +
        " webkitBackdrop=" + css("-webkit-backdrop-filter", "blur(4px)") + " zoomProp=" + css("zoom", "1.5"));
    var types = ['video/mp4; codecs="hvc1.2.4.L153.B0"', 'video/mp4; codecs="avc1.640028"', 'video/webm; codecs="vp9"',
      'video/x-matroska', 'video/mp4; codecs="mp4v.20.9"', 'audio/flac', 'audio/mpeg', 'audio/mp4; codecs="alac"',
      'audio/x-wavpack', 'audio/webm; codecs="opus"', 'audio/mp4; codecs="mp4a.40.2"', 'audio/ogg; codecs="flac"'];
    var out = [];
    for (var i = 0; i < types.length; i++) { out.push(types[i] + "=" + (v.canPlayType(types[i]) || "no")); }
    log("canPlayType " + out.join(" | "));
    log("api fetch=" + (typeof window.fetch) + " MediaSource=" + (typeof window.MediaSource) +
        " mediaCapabilities=" + (typeof navigator.mediaCapabilities) + " audioTracks=" + (typeof v.audioTracks) +
        " quality=" + (typeof v.getVideoPlaybackQuality) + " decodedBytes=" + (typeof v.webkitAudioDecodedByteCount));
  }

  function u1() {
    var m = window.ltvModern;
    if (m) {
      var c = [];
      for (var k in m.checks) { c.push(k + "=" + m.checks[k]); }
      log("U1 modern module ran: ok=" + m.ok + " " + c.join(" "));
    } else {
      log("U1 modern module did NOT run (nomodule=" + !!window.ltvNoModule + ", js errors so far " + errors + ")");
    }
  }

  // ---------------------------------------------------------------- U11 keys, pointer, life cycle
  var backAt = [];
  function keyLine(s) {
    var el = $("keys");
    var old = el.textContent.indexOf("keys:") === 0 ? [] : el.textContent.split("\n");
    old.unshift(s);
    el.textContent = old.slice(0, 6).join("\n");
  }
  function close() {
    var palm = window.PalmSystem;
    var how = palm && palm.platformBack ? "PalmSystem.platformBack()" : "window.close()";
    log("exit: Back x3 -> " + how);
    setTimeout(function () { if (palm && palm.platformBack) { palm.platformBack(); } else { window.close(); } }, 500);
  }
  document.addEventListener("keydown", function (e) {
    keysSeen += 1;
    var s = "down " + e.keyCode + " key=" + e.key + " code=" + e.code + (e.repeat ? " repeat" : "");
    keyLine(s);
    log("KEY " + s);
    if (e.keyCode === 461 || e.keyCode === 27 || e.keyCode === 8) {
      e.preventDefault();
      backAt.push(now());
      backAt = backAt.filter(function (t) { return now() - t < 2000; });
      if (backAt.length >= 3) { close(); }
    }
  }, true);
  document.addEventListener("keyup", function (e) { log("KEY up " + e.keyCode); }, true);
  var lastMove = 0;
  document.addEventListener("mousemove", function (e) {
    if (now() - lastMove > 1000) { lastMove = now(); log("PTR move " + e.clientX + "," + e.clientY); }
  }, true);
  document.addEventListener("click", function (e) { log("PTR click " + e.clientX + "," + e.clientY + " button " + e.button); }, true);
  document.addEventListener("wheel", function (e) {
    log("PTR wheel dy=" + e.deltaY + " dx=" + e.deltaX + " mode=" + e.deltaMode);
  }, { capture: true, passive: true });
  document.addEventListener("cursorStateChange", function (e) {
    log("PTR cursorStateChange " + JSON.stringify(e.detail || null));
  }, true);
  document.addEventListener("keyboardStateChange", function (e) {
    log("EVT keyboardStateChange " + JSON.stringify(e.detail || null));
  }, true);
  document.addEventListener("webOSRelaunch", function (e) {
    log("EVT webOSRelaunch " + JSON.stringify(e.detail || null));
  }, true);
  document.addEventListener("visibilitychange", function () {
    log("VIS hidden=" + document.hidden + " state=" + document.visibilityState);
  });
  window.addEventListener("blur", function () { log("EVT window blur"); });
  window.addEventListener("focus", function () { log("EVT window focus"); });

  // ---------------------------------------------------------------- library + sample choice
  function getJSON(url, ok, fail) {
    var x = new XMLHttpRequest();
    x.open("GET", url);
    x.onload = function () {
      if (x.status !== 200) { fail("HTTP " + x.status); return; }
      try { ok(JSON.parse(x.responseText)); } catch (e) { fail("bad JSON"); }
    };
    x.onerror = function () { fail("network"); };
    x.send();
  }
  function has(it, badge) { return (it.badges || []).indexOf(badge) >= 0; }
  function shortest(filter) {
    var best = null;
    for (var i = 0; i < lib.length; i++) {
      var it = lib[i];
      if (it.duration && filter(it) && (!best || (!!best.hidden > !!it.hidden) ||
          (!!best.hidden === !!it.hidden && it.duration < best.duration))) { best = it; }
    }
    return best;
  }
  var CLASSES = [
    ["HEVC 4K 50/60", function (i) { return i.kind === "video" && i.codec === "hevc" && i.height >= 2000 && i.fps >= 48 && i.dyn !== "hlg"; }],
    ["HEVC 4K 25/30", function (i) { return i.kind === "video" && i.codec === "hevc" && i.height >= 2000 && i.fps < 48; }],
    ["HEVC 1080", function (i) { return i.kind === "video" && i.codec === "hevc" && i.height >= 1000 && i.height <= 1100; }],
    ["VP9", function (i) { return i.kind === "video" && i.codec === "vp9"; }],
    ["H.264", function (i) { return i.kind === "video" && i.codec === "h264"; }],
    ["MPEG-4", function (i) { return i.kind === "video" && i.codec === "mpeg4"; }],
    ["HLG", function (i) { return i.kind === "video" && i.dyn === "hlg"; }],
    ["video Opus", function (i) { return i.kind === "video" && i.acodec === "opus"; }],
    ["video FLAC", function (i) { return i.kind === "video" && i.acodec === "flac"; }],
    ["video MP3", function (i) { return i.kind === "video" && i.acodec === "mp3"; }],
    ["video AAC", function (i) { return i.kind === "video" && i.acodec === "aac"; }],
    ["song FLAC 16/44", function (i) { return i.kind === "audio" && has(i, "FLAC 16/44"); }],
    ["song FLAC 24/96", function (i) { return i.kind === "audio" && has(i, "FLAC 24/96"); }],
    ["song FLAC 24/192", function (i) { return i.kind === "audio" && has(i, "FLAC 24/192"); }],
    ["song FLAC 5.1", function (i) { return i.kind === "audio" && i.codec === "flac" && has(i, "5.1"); }],
    ["song MP3", function (i) { return i.kind === "audio" && i.codec === "mp3"; }],
    ["song ALAC", function (i) { return i.kind === "audio" && i.codec === "alac"; }],
    ["song WavPack", function (i) { return i.kind === "audio" && i.codec === "wv"; }]
  ];
  function choose() {
    var byId = {}, only = Q.only ? Q.only.toLowerCase().split(",") : null;
    for (var c = 0; c < CLASSES.length; c++) {
      var it = shortest(CLASSES[c][1]);
      if (!it) { log("class " + CLASSES[c][0] + ": no item in the library"); continue; }
      if (byId[it.id]) { byId[it.id].label += " + " + CLASSES[c][0]; continue; }
      byId[it.id] = { label: CLASSES[c][0], item: it, ord: 0 };
      samples.push(byId[it.id]);
    }
    if (only) {
      samples = samples.filter(function (s) {
        for (var i = 0; i < only.length; i++) { if (s.label.toLowerCase().indexOf(only[i]) >= 0) { return true; } }
        return false;
      });
    }
  }
  // Videos whose default audio track is not the first one (U3): needs the full items.
  function findMultiTrack(done) {
    var vids = lib.filter(function (i) { return i.kind === "video"; }), left = vids.length, best = null, k = 0;
    if (!left || (Q.only && Q.only.toLowerCase().indexOf("multi") < 0)) { done(); return; }
    function next() {
      if (k >= vids.length) { return; }
      var it = vids[k++];
      getJSON("/api/item/" + it.id, function (full) {
        if (full.audio_ord > 0 && (!best || it.duration < best.item.duration)) { best = { item: it, ord: full.audio_ord, track: full.audio_track }; }
        finish();
      }, finish);
    }
    function finish() {
      left -= 1;
      if (left === 0) {
        if (best) {
          samples.push({ label: "multi-track (default a:" + best.ord + " " + best.track + ")", item: best.item, ord: best.ord });
        } else {
          log("U3 no video with a non-first default audio track");
        }
        done();
      } else { next(); }
    }
    for (var i = 0; i < 6; i++) { next(); }
  }

  // ---------------------------------------------------------------- one sample
  function bytes() {
    return { ab: v.webkitAudioDecodedByteCount, vb: v.webkitVideoDecodedByteCount };
  }
  function snap() {
    var q = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null, b = bytes(), buf = [];
    for (var i = 0; i < v.buffered.length; i++) { buf.push(v.buffered.start(i).toFixed(0) + "-" + v.buffered.end(i).toFixed(0)); }
    return "t=" + v.currentTime.toFixed(1) + " ab=" + b.ab + " vb=" + b.vb +
      (q ? " drop=" + q.droppedVideoFrames + "/" + q.totalVideoFrames : "") + " rs=" + v.readyState + " buf=" + buf.join(",");
  }
  function tracks() {
    var a = v.audioTracks, out = [];
    if (!a) { return "audioTracks=none"; }
    for (var i = 0; i < a.length; i++) {
      out.push(i + ":" + (a[i].label || "") + "/" + (a[i].language || "") + (a[i].enabled ? "*" : ""));
    }
    return "audioTracks=" + a.length + "[" + out.join(" ") + "] videoTracks=" + (v.videoTracks ? v.videoTracks.length : "none");
  }
  function testSample(s, opts, done) {
    var it = s.item, r = [], start = now(), first = 0, finished = false, timers = [], handlers = [];
    var waiting = 0, stalled = 0;
    function later(fn, ms) { timers.push(setTimeout(fn, ms)); }
    function on(ev, fn) { v.addEventListener(ev, fn); handlers.push([ev, fn]); }
    function finish(why) {
      if (finished) { return; }
      finished = true;
      for (var i = 0; i < timers.length; i++) { clearTimeout(timers[i]); }
      for (var j = 0; j < handlers.length; j++) { v.removeEventListener(handlers[j][0], handlers[j][1]); }
      v.pause();
      if (opts.overlay) { $("glass").style.display = $("fade").style.display = "none"; }
      log("S " + s.label + " #" + it.id + " " + (it.badges || []).join("/") + " -> " + why + " | " + r.join(" | ") +
          " | waiting=" + waiting + " stalled=" + stalled);
      setTimeout(function () { done(why); }, 400);
    }
    step(s.label, "#" + it.id + " " + (it.title || "") + " · " + (it.badges || []).join(" "));
    on("loadedmetadata", function () {
      r.push("meta " + (now() - start) + "ms dur=" + (v.duration || 0).toFixed(1) + "/" + (it.duration || 0).toFixed(1) +
             " size=" + v.videoWidth + "x" + v.videoHeight + " " + tracks());
    });
    on("waiting", function () { waiting += 1; });
    on("stalled", function () { stalled += 1; });
    on("error", function () {
      var e = v.error;
      r.push("MEDIA_ERR " + (e ? e.code + " " + (e.message || "") : "?"));
      finish("error");
    });
    on("playing", function () {
      if (first) { return; }
      first = now() - start;
      r.push("first frame " + first + "ms");
      if (opts.overlay) { $("glass").style.display = $("fade").style.display = "block"; log("U8 strips shown over " + s.label); }
      if (opts.onPlaying) { opts.onPlaying(finish); return; }
      later(afterPlay, play(6000));
    });
    v.muted = false;
    v.volume = 1;
    v.src = "/media/" + it.id;
    var p = v.play();
    if (p && p.then) {
      p["catch"](function (e) {
        if (e && e.name === "AbortError") { return; }
        r.push("play() rejected " + (e && e.name) + " " + (e && e.message));
        if (e && e.name !== "NotAllowedError") { finish("play rejected"); }
      });
    }
    later(function () { if (!v.readyState) { finish("no metadata in 15 s"); } }, 15000);
    later(function () { if (!first) { finish("no playing in 30 s"); } }, 30000);

    function afterPlay() {
      r.push("A " + snap());
      var a = v.audioTracks;
      if (s.ord > 0 && a && a.length > s.ord) {
        var before = bytes().ab;
        for (var i = 0; i < a.length; i++) { a[i].enabled = i === s.ord; }
        later(function () { r.push("track " + s.ord + " enabled: ab +" + (bytes().ab - before) + " " + tracks()); seekMid(); }, play(3000));
      } else {
        if (s.ord > 0) { r.push("track switch impossible: " + tracks()); }
        seekMid();
      }
    }
    function seekMid() {
      var target = (v.duration || it.duration) * 0.5, t = now(), seeked = 0;
      var h = function () { seeked = now() - t; v.removeEventListener("seeked", h); };
      v.addEventListener("seeked", h);
      v.currentTime = target;
      var poll = setInterval(function () {
        if (finished) { clearInterval(poll); return; }
        if (v.currentTime > target + 0.5) {
          clearInterval(poll);
          r.push("seek 50% seeked " + seeked + "ms running " + (now() - t) + "ms");
          later(function () { r.push("B " + snap()); seekEnd(); }, play(3000));
        } else if (now() - t > 15000) {
          clearInterval(poll);
          r.push("seek 50% seeked " + seeked + "ms NOT running after 15 s " + snap());
          seekEnd();
        }
      }, 100);
    }
    function seekEnd() {
      var d = v.duration || it.duration;
      on("ended", function () { r.push("ended at " + v.currentTime.toFixed(1) + "/" + d.toFixed(1)); finish("ok"); });
      v.currentTime = Math.max(0, d - 4);
      if (v.paused) { v.play(); }
      later(function () { r.push("NO ended 15 s after a seek to the end, t=" + v.currentTime.toFixed(1)); finish("no ended"); }, 15000);
    }
  }

  // ---------------------------------------------------------------- U6 autoplay
  function autoplay(done) {
    var song = shortest(function (i) { return i.kind === "audio" && has(i, "FLAC 16/44"); }) ||
      shortest(function (i) { return i.kind === "audio"; });
    if (!song) { log("U6 no song for the autoplay test"); done(); return; }
    step("Autoplay test", "starts a song without a key press");
    var pressed = keysSeen;
    v.src = "/media/" + song.id;
    var p = v.play();
    function stop() { setTimeout(function () { v.pause(); done(); }, play(3000)); }
    if (!p || !p.then) { log("U6 play() returned no promise"); stop(); return; }
    p.then(function () {
      log("U6 autoplay without a key press: OK (keys pressed before: " + pressed + ")");
      stop();
    })["catch"](function (e) {
      log("U6 autoplay without a key press: " + (e && e.name) + " " + (e && e.message) + " (keys pressed before: " + pressed + ")");
      v.muted = true;
      var m = v.play();
      if (m && m.then) {
        m.then(function () { log("U6 muted autoplay: OK"); v.muted = false; stop(); })["catch"](function (e2) {
          log("U6 muted autoplay: " + (e2 && e2.name)); v.muted = false; done();
        });
      } else { v.muted = false; done(); }
    });
  }

  // ---------------------------------------------------------------- U7 FPS of a moving shelf (plain / over a 4K video)
  function shelfFps(label, ms, done) {
    var sh = $("shelf");
    if (!sh.children.length) { for (var i = 0; i < 12; i++) { sh.appendChild(document.createElement("div")); } }
    sh.style.display = "block";
    sh.className = "move";
    var frames = 0, worst = 0, last = 0, startT = 0, flip = false;
    var mover = setInterval(function () { flip = !flip; sh.style.transform = flip ? "translateX(-1320px)" : "translateX(0)"; }, 1000);
    function tick(ts) {
      if (!startT) { startT = ts; last = ts; }
      worst = Math.max(worst, ts - last);
      last = ts;
      frames += 1;
      if (ts - startT < ms) { requestAnimationFrame(tick); return; }
      clearInterval(mover);
      sh.style.display = "none";
      log("U7 FPS " + label + ": " + (frames * 1000 / (ts - startT)).toFixed(1) + " fps, worst frame " + worst.toFixed(0) + "ms");
      done();
    }
    requestAnimationFrame(tick);
  }
  function fpsTests(done) {
    step("UI speed", "moving test shelf");
    shelfFps("shelf over black", play(4000), function () {
      var s4k = samples.filter(function (s) { return s.label.indexOf("HEVC 4K 50") === 0; })[0];
      if (!s4k) { done(); return; }
      testSample(s4k, {
        onPlaying: function (finish) {
          setTimeout(function () {
            var q0 = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null;
            shelfFps("shelf over the playing 4K 50 video", play(5000), function () {
              var q1 = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null;
              if (q0 && q1) {
                log("U7 video frames during the shelf: dropped +" + (q1.droppedVideoFrames - q0.droppedVideoFrames) +
                    " of +" + (q1.totalVideoFrames - q0.totalVideoFrames));
              }
              finish("fps done");
            });
          }, play(3000));
        }
      }, function () { done(); });
    });
  }

  // ---------------------------------------------------------------- U10 paused for a while
  function pausedTest(done) {
    var s = samples.filter(function (x) { return x.label.indexOf("HEVC 1080") === 0; })[0] || samples[0];
    var secs = Q.pause ? +Q.pause : 240;
    if (!s || secs <= 0) { done(); return; }
    testSample(s, {
      onPlaying: function (finish) {
        setTimeout(function () {
          v.pause();
          step("Paused for " + Math.round(secs / 60) + " minutes", "does the TV's own screen saver start?");
          log("U10 paused at t=" + v.currentTime.toFixed(1) + " for " + secs + " s");
          var n = 0;
          var iv = setInterval(function () {
            n += 30;
            log("U10 paused " + n + " s hidden=" + document.hidden + " state=" + document.visibilityState + " paused=" + v.paused);
            if (n >= secs) { clearInterval(iv); finish("pause done"); }
          }, FAST ? 300 : 30000);
        }, play(3000));
      }
    }, function () { done(); });
  }

  // ---------------------------------------------------------------- run
  function series(i, ok, done) {
    if (i >= samples.length) { done(ok); return; }
    var s = samples[i];
    testSample(s, { overlay: s.label.indexOf("HEVC 1080") === 0 }, function (why) {
      series(i + 1, ok + (why === "ok" ? 1 : 0), done);
    });
  }
  function countdown(secs, done) {
    if (secs <= 0) { done(); return; }
    step("Key test", "the media series starts in " + secs + " s");
    setTimeout(function () { countdown(secs - 1, done); }, FAST ? 50 : 1000);
  }
  function run() {
    autoplay(function () {
      if (Q.series === "0") { step("Probe ready", "keys and pointer are logged"); log("series skipped"); return; }
      countdown(25, function () {
        log("series: " + samples.length + " samples: " + samples.map(function (s) { return s.label + " #" + s.item.id; }).join(", "));
        series(0, 0, function (ok) {
          log("series done: " + ok + " of " + samples.length + " ok");
          fpsTests(function () {
            pausedTest(function () {
              v.removeAttribute("src");
              v.load();
              step("Probe finished", "press Back three times quickly to close the app");
              log("probe finished, js errors " + errors);
            });
          });
        });
      });
    });
  }

  connect();
  env();
  setTimeout(u1, 3000);
  step("LTVPlayer probe", "loading the library…");
  getJSON("/api/library?all=1", function (data) {
    lib = data.items || [];
    log("library " + lib.length + " items");
    choose();
    findMultiTrack(function () {
      log("samples " + samples.length);
      run();
    });
  }, function (why) { log("library failed: " + why); step("Library failed", why); });
})();

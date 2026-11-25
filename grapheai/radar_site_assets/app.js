/* PV Radar Site: search, list filters and chart tooltips. No dependencies.
   Every string from the data files is written with textContent, never as
   markup, and only http(s) links are followed. */
(function () {
  "use strict";
  var ROOT = document.body.getAttribute("data-root") || "";
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                "Sep", "Oct", "Nov", "Dec"];
  var PAGE = 60;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function fmtDate(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    return m ? (+m[3]) + " " + MONTHS[+m[2] - 1] + " " + m[1] : "";
  }
  function link(text, url, internal) {
    if (!internal && !/^https?:\/\//i.test(url || "")) return el("span", "", text);
    var a = el("a", internal ? "" : "out", text);
    a.href = internal ? ROOT + url : url;
    if (!internal) a.rel = "noopener noreferrer";
    return a;
  }
  function load(name) {
    return fetch(ROOT + "data/" + name).then(function (r) {
      if (!r.ok) throw new Error(name + " " + r.status);
      return r.json();
    });
  }
  function hay(r) {
    return [r.t, r.s, r.j, r.a, r.g, (r.c || []).join(" "),
            (r.tp || []).join(" ")].join(" ").toLowerCase();
  }
  function tokens(q) {
    return q.toLowerCase().split(/\s+/).filter(Boolean);
  }
  function matches(h, toks) {
    for (var i = 0; i < toks.length; i++) if (h.indexOf(toks[i]) < 0) return false;
    return true;
  }
  function join(parts) {
    return parts.filter(Boolean).join(" · ");
  }

  function entry(r, kind) {
    var li = el("li", "entry");
    if (kind === "news") {
      var tags = el("div", "tags");
      tags.appendChild(el("span", "sig sig-" + (r.g || "news").toLowerCase(), r.g || "News"));
      if (r.e != null) {
        var f = el("span", "fact");
        f.appendChild(el("b", "", r.e + "%"));
        f.appendChild(document.createTextNode(" " + (r.ct || "").toLowerCase()));
        tags.appendChild(f);
      }
      if (r.f) {
        var g = el("span", "fact");
        g.appendChild(el("b", "", r.f));
        g.appendChild(document.createTextNode(" funding"));
        tags.appendChild(g);
      }
      li.appendChild(tags);
      li.appendChild(link(r.t, r.u));
      li.appendChild(el("span", "meta", join([r.s, (r.c || []).join(", ")])));
    } else {
      li.appendChild(link(r.t, r.u));
      li.appendChild(el("span", "meta", join([r.j, r.a])));
    }
    return li;
  }

  /* ---- news and papers: search, facets, paging */
  function finder(box) {
    var kind = box.getAttribute("data-kind");
    var list = box.querySelector("[data-list]");
    var count = box.querySelector("[data-count]");
    var more = box.querySelector("[data-more]");
    var input = box.querySelector("input[type=search]");
    var facets = [].slice.call(box.querySelectorAll("[data-facet]"));
    var rows = [], picked = {}, shown = PAGE;

    function current() {
      var toks = tokens(input.value);
      return rows.filter(function (r) {
        for (var k in picked) {
          if (!picked[k]) continue;
          var v = r[k];
          if (Array.isArray(v) ? v.indexOf(picked[k]) < 0 : v !== picked[k]) return false;
        }
        return matches(r._h, toks);
      });
    }
    function draw() {
      var hits = current();
      list.textContent = "";
      var day = null;
      hits.slice(0, shown).forEach(function (r) {
        if (r.d !== day) {
          day = r.d;
          list.appendChild(el("li", "day", fmtDate(r.d) || "Undated"));
        }
        list.appendChild(entry(r, kind));
      });
      if (!hits.length) list.appendChild(el("li", "empty", "Nothing matches. Clear a filter or shorten the search."));
      count.textContent = hits.length.toLocaleString() + " of " +
        rows.length.toLocaleString() + (kind === "news" ? " items" : " papers");
      more.hidden = hits.length <= shown;
    }
    function buildFacet(node) {
      var key = node.getAttribute("data-facet");
      var tally = {};
      rows.forEach(function (r) {
        [].concat(r[key] || []).forEach(function (v) {
          if (v) tally[v] = (tally[v] || 0) + 1;
        });
      });
      var names = Object.keys(tally).sort(function (a, b) {
        return tally[b] - tally[a] || a.localeCompare(b);
      }).slice(0, 12);
      names.forEach(function (name) {
        var b = el("button", "", name);
        b.type = "button";
        b.setAttribute("aria-pressed", "false");
        b.appendChild(el("span", "", String(tally[name])));
        b.addEventListener("click", function () {
          var on = picked[key] === name;
          picked[key] = on ? null : name;
          [].forEach.call(node.querySelectorAll("button"), function (x) {
            x.setAttribute("aria-pressed", String(!on && x === b));
          });
          shown = PAGE;
          draw();
        });
        node.appendChild(b);
      });
      if (!names.length) node.hidden = true;
    }
    load(box.getAttribute("data-src").replace(/^data\//, "")).then(function (data) {
      rows = data;
      rows.forEach(function (r) { r._h = hay(r); });
      facets.forEach(buildFacet);
      input.addEventListener("input", function () { shown = PAGE; draw(); });
      more.addEventListener("click", function () { shown += PAGE; draw(); });
      draw();
    }).catch(function () {
      /* The server-rendered newest items stay in place. */
      count.textContent = "Showing the newest items. Search needs the site to be served over http.";
    });
  }

  /* ---- companies: filter the cards already on the page */
  function filter(box) {
    var input = box.querySelector("input[type=search]");
    var cards = [].slice.call(box.querySelectorAll("[data-text]"));
    var count = box.querySelector("[data-count]");
    input.addEventListener("input", function () {
      var toks = tokens(input.value), n = 0;
      cards.forEach(function (c) {
        var ok = matches(c.getAttribute("data-text"), toks);
        c.hidden = !ok;
        if (ok) n++;
      });
      count.textContent = toks.length ? n + " of " + cards.length + " organisations" : "";
    });
  }

  /* ---- overview: one search across companies, news and papers */
  function search(form) {
    var input = form.querySelector("input");
    var out = document.querySelector("[data-results]");
    var data = null, loading = null;
    function ensure() {
      if (!loading) {
        loading = Promise.all([load("companies.json"), load("news.json"), load("papers.json")])
          .then(function (d) {
            d.forEach(function (rows) { rows.forEach(function (r) { r._h = hay(r); }); });
            data = d;
          });
      }
      return loading;
    }
    function group(title, rows, limit, render) {
      if (!rows.length) return;
      out.appendChild(el("h3", "", title + " (" + rows.length.toLocaleString() + ")"));
      var ul = el("ul");
      rows.slice(0, limit).forEach(function (r) { ul.appendChild(render(r)); });
      out.appendChild(ul);
    }
    function run() {
      var toks = tokens(input.value);
      out.textContent = "";
      if (!toks.length || !data) { out.hidden = true; return; }
      var hit = data.map(function (rows) {
        return rows.filter(function (r) { return matches(r._h, toks); });
      });
      group("Companies and institutes", hit[0], 5, function (r) {
        var li = el("li");
        li.appendChild(link(r.t, r.u, true));
        li.appendChild(el("span", "meta", r.s));
        return li;
      });
      group("News", hit[1], 8, function (r) {
        var li = el("li");
        li.appendChild(link(r.t, r.u));
        li.appendChild(el("span", "meta", join([r.g, r.s, fmtDate(r.d)])));
        return li;
      });
      group("Papers", hit[2], 6, function (r) {
        var li = el("li");
        li.appendChild(link(r.t, r.u));
        li.appendChild(el("span", "meta", join([r.j, r.a, fmtDate(r.d)])));
        return li;
      });
      if (!out.childNodes.length) out.appendChild(el("p", "quiet", "Nothing in the store matches."));
      out.hidden = false;
    }
    form.addEventListener("submit", function (e) { e.preventDefault(); });
    input.addEventListener("focus", function () { ensure().catch(function () {}); });
    input.addEventListener("input", function () {
      ensure().then(run).catch(function () {
        out.textContent = "";
        out.appendChild(el("p", "quiet", "Search needs the site to be served over http."));
        out.hidden = false;
      });
    });
  }

  /* ---- chart tooltips: hover and keyboard focus */
  function tooltips() {
    var tip = null;
    function show(target, x, y) {
      if (!tip) { tip = el("div", "tip"); tip.setAttribute("role", "tooltip"); document.body.appendChild(tip); }
      tip.textContent = target.getAttribute("data-tip");
      tip.hidden = false;
      var w = tip.offsetWidth, h = tip.offsetHeight;
      var left = Math.min(Math.max(8, x + 12), window.innerWidth - w - 8);
      var top = y - h - 12 < 8 ? y + 16 : y - h - 12;
      tip.style.left = left + "px";
      tip.style.top = top + "px";
    }
    function hide() { if (tip) tip.hidden = true; }
    document.addEventListener("mousemove", function (e) {
      var t = e.target.closest ? e.target.closest("[data-tip]") : null;
      if (t) show(t, e.clientX, e.clientY); else hide();
    });
    document.addEventListener("focusin", function (e) {
      var t = e.target.closest ? e.target.closest("[data-tip]") : null;
      if (!t) return hide();
      var r = t.getBoundingClientRect();
      show(t, r.left + r.width / 2, r.top);
    });
    document.addEventListener("focusout", hide);
    document.addEventListener("scroll", hide, true);
  }

  [].forEach.call(document.querySelectorAll("[data-finder]"), finder);
  [].forEach.call(document.querySelectorAll("[data-filter]"), filter);
  [].forEach.call(document.querySelectorAll("[data-search]"), search);
  tooltips();
})();

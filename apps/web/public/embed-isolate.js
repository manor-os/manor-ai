// A classic script loaded by index.html before any module, so it runs before
// every bundled chunk evaluates. In the embedded customer-trial frame
// (/embed/<kind>/<id>/..., framed by this same origin) it replaces Web Storage
// with memory: the frame shares this origin's storage with the provider's own
// session in the parent page, and the customer token, user and caches must
// never land there. src/embed/embedContext.ts reads the marker it leaves and
// refuses to boot an embed URL without it (fail closed).
(function () {
  "use strict";
  var match = /^\/embed\/(client-trial|collaboration)\/([0-9A-Za-z]{26})(?=\/|$)/.exec(window.location.pathname);
  if (!match) return;
  var framed = false;
  try {
    framed = window.parent !== window && window.parent.location.origin === window.location.origin;
  } catch (error) {
    framed = false;
  }
  if (!framed) return;

  function MemoryStorage() {
    var values = new Map();
    Object.defineProperties(this, {
      length: { get: function () { return values.size; } },
      key: { value: function (index) { var keys = Array.from(values.keys()); return index in keys ? keys[index] : null; } },
      getItem: { value: function (key) { key = String(key); return values.has(key) ? values.get(key) : null; } },
      setItem: { value: function (key, value) { values.set(String(key), String(value)); } },
      removeItem: { value: function (key) { values.delete(String(key)); } },
      clear: { value: function () { values.clear(); } },
    });
  }

  var local = new MemoryStorage();
  var tab = new MemoryStorage();
  // Display preferences only; never a credential.
  try {
    ["manor_locale", "manor-theme-preference-v2"].forEach(function (key) {
      var value = window.localStorage.getItem(key);
      if (value !== null) local.setItem(key, value);
    });
  } catch (error) {
    // Blocked storage: the frame starts with defaults.
  }

  var isolated = true;
  [["localStorage", local], ["sessionStorage", tab]].forEach(function (entry) {
    try {
      Object.defineProperty(window, entry[0], { configurable: true, enumerable: true, get: function () { return entry[1]; } });
    } catch (error) {
      isolated = false;
    }
    if (window[entry[0]] !== entry[1]) isolated = false;
  });
  if (!isolated) return;
  // The parent's writes to the real storage reach this window as storage
  // events; they describe a session this frame does not use.
  window.addEventListener("storage", function (event) { event.stopImmediatePropagation(); }, true);
  Object.defineProperty(window, "__manorEmbed", {
    value: Object.freeze({ kind: match[1], refId: match[2], basename: match[0], localStorage: local, sessionStorage: tab }),
  });
})();

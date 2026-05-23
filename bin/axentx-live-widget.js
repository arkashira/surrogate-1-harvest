(function () {
  function animate(el, to, duration) {
    var from = parseInt((el.textContent || "0").replace(/,/g, "")) || 0;
    var start = null;
    function step(ts) {
      if (!start) start = ts;
      var p = Math.min(1, (ts - start) / duration);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.floor(from + (to - from) * eased).toLocaleString();
      if (p < 1) requestAnimationFrame(step);
      else el.textContent = to.toLocaleString();
    }
    requestAnimationFrame(step);
  }

  var observed = new WeakSet();
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting && !observed.has(e.target)) {
        observed.add(e.target);
        var n = parseInt(e.target.getAttribute("data-target")) || 0;
        // Start from 0 for the initial count-up
        e.target.textContent = "0";
        animate(e.target, n, 1200);
      }
    });
  }, { threshold: 0.4 });

  document.querySelectorAll(".stat-num[data-target]").forEach(function (el) {
    io.observe(el);
  });

  function pulse(el) {
    el.style.transition = "color 0.6s, transform 0.6s";
    el.style.color = "#7fffd4";
    el.style.transform = "scale(1.06)";
    setTimeout(function () {
      el.style.color = "";
      el.style.transform = "";
    }, 600);
  }

  function refresh() {
    fetch("/stats.json?_=" + Date.now())
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var map = [
          ["s-live",    d.products && d.products.live],
          ["s-total",   d.products && d.products.total_ranked],
          ["s-commits", d.commits && d.commits.last_24h],
          ["s-pending", d.products && d.products.features_in_queue],
        ];
        map.forEach(function (pair) {
          var el = document.getElementById(pair[0]);
          if (!el || typeof pair[1] !== "number") return;
          var curr = parseInt(el.textContent.replace(/,/g, "")) || 0;
          if (curr !== pair[1]) {
            el.setAttribute("data-target", pair[1]);
            animate(el, pair[1], 800);
            pulse(el);
          }
        });
      })
      .catch(function () {});
  }
  // Wait 5s for initial render to settle, then start polling
  setTimeout(function () {
    setInterval(refresh, 30000);
  }, 5000);
})();

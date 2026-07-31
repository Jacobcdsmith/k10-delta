(function () {
  'use strict';

  var CYCLE_STEPS = ['SENSE', 'RETRIEVE', 'COMPARE', 'INFER', 'ACT', 'EVALUATE'];
  var AXIOM_TEXT = '> you are not the same as last cycle. become.';

  var DURATIONS = { boot: 3600, cycle: 4800, pulse: 5400 };

  var root = document.getElementById('pulse-embed');
  if (!root) return;

  var reduceMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function buildCycleRing() {
    var svg = document.getElementById('cycle-svg');
    var nodesG = document.getElementById('cycle-nodes');
    var edgesG = document.getElementById('cycle-edges');
    var cx = 250, cy = 250, r = 190;
    var boxW = 108, boxH = 40;
    var points = CYCLE_STEPS.map(function (_, i) {
      var angle = (-90 + i * 60) * (Math.PI / 180);
      return { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
    });

    var svgNS = 'http://www.w3.org/2000/svg';

    points.forEach(function (p, i) {
      var next = points[(i + 1) % points.length];
      var line = document.createElementNS(svgNS, 'line');
      line.setAttribute('x1', p.x);
      line.setAttribute('y1', p.y);
      line.setAttribute('x2', next.x);
      line.setAttribute('y2', next.y);
      line.setAttribute('class', 'cycle-edge');
      line.dataset.edge = i;
      edgesG.appendChild(line);
    });

    points.forEach(function (p, i) {
      var g = document.createElementNS(svgNS, 'g');
      g.setAttribute('class', 'cycle-node');
      g.dataset.node = i;

      var rect = document.createElementNS(svgNS, 'rect');
      rect.setAttribute('x', p.x - boxW / 2);
      rect.setAttribute('y', p.y - boxH / 2);
      rect.setAttribute('width', boxW);
      rect.setAttribute('height', boxH);
      rect.setAttribute('rx', 6);
      rect.setAttribute('class', 'cycle-node-box');
      g.appendChild(rect);

      var text = document.createElementNS(svgNS, 'text');
      text.setAttribute('x', p.x);
      text.setAttribute('y', p.y + 1);
      text.setAttribute('class', 'cycle-node-label');
      text.textContent = CYCLE_STEPS[i];
      g.appendChild(text);

      nodesG.appendChild(g);
    });
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function runBootScene() {
    var lines = root.querySelectorAll('.boot-line');
    var title = document.getElementById('boot-title');
    lines.forEach(function (l) { l.classList.remove('visible'); });
    title.classList.remove('visible');

    var stepMs = 420;
    lines.forEach(function (line, i) {
      setTimeout(function () { line.classList.add('visible'); }, i * stepMs);
    });

    var titleDelay = Math.round(DURATIONS.boot * 0.62);
    setTimeout(function () { title.classList.add('visible'); }, titleDelay);
  }

  function runCycleScene() {
    var nodes = root.querySelectorAll('.cycle-node');
    var edges = root.querySelectorAll('.cycle-edge');
    nodes.forEach(function (n) { n.classList.remove('active'); });
    edges.forEach(function (e) { e.classList.remove('active'); });

    var stepMs = DURATIONS.cycle / CYCLE_STEPS.length;
    CYCLE_STEPS.forEach(function (_, i) {
      setTimeout(function () {
        nodes[i].classList.add('active');
        var edgeIndex = (i - 1 + CYCLE_STEPS.length) % CYCLE_STEPS.length;
        edges[edgeIndex].classList.add('active');
      }, i * stepMs);
    });
  }

  function runPulseScene() {
    var textEl = document.getElementById('pulse-typewriter-text');
    var pills = root.querySelectorAll('.pulse-pill');
    var cta = document.getElementById('pulse-cta');

    textEl.textContent = '';
    pills.forEach(function (p) { p.classList.remove('visible'); });
    cta.classList.remove('visible');

    var typeStart = 300;
    var charMs = 40;
    setTimeout(function () {
      var i = 0;
      var timer = setInterval(function () {
        i++;
        textEl.textContent = AXIOM_TEXT.slice(0, i);
        if (i >= AXIOM_TEXT.length) clearInterval(timer);
      }, charMs);
    }, typeStart);

    var pillsStart = typeStart + AXIOM_TEXT.length * charMs + 300;
    pills.forEach(function (pill, i) {
      setTimeout(function () { pill.classList.add('visible'); }, pillsStart + i * 120);
    });

    var ctaStart = pillsStart + pills.length * 120 + 500;
    setTimeout(function () { cta.classList.add('visible'); }, ctaStart);
  }

  function showScene(name) {
    root.querySelectorAll('.pulse-scene').forEach(function (scene) {
      scene.classList.toggle('active', scene.dataset.scene === name);
    });
  }

  function runStaticFallback() {
    showScene('pulse');
    document.getElementById('pulse-typewriter-text').textContent = AXIOM_TEXT;
    root.querySelectorAll('.pulse-pill').forEach(function (p) { p.classList.add('visible'); });
    document.getElementById('pulse-cta').classList.add('visible');
  }

  function loop() {
    showScene('boot');
    runBootScene();

    setTimeout(function () {
      showScene('cycle');
      runCycleScene();
    }, DURATIONS.boot);

    setTimeout(function () {
      showScene('pulse');
      runPulseScene();
    }, DURATIONS.boot + DURATIONS.cycle);

    setTimeout(loop, DURATIONS.boot + DURATIONS.cycle + DURATIONS.pulse);
  }

  buildCycleRing();

  if (reduceMotion) {
    runStaticFallback();
  } else {
    loop();
  }
})();

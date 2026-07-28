/* ============================================================
 * effects.js — 鼠标流星拖尾特效（Portal）
 * ------------------------------------------------------------
 * 全屏透明 canvas（pointer-events:none 不挡点击），监听 mousemove
 * 在指针处喷发粒子，带初速度 + 重力 + 透明度衰减，拖出流星尾迹。
 * 配色为适配浅色背景调过：偏饱和的蓝紫/青，白底上仍有光晕。
 *
 * 性能护栏：
 *   - prefers-reduced-motion: reduce / 触摸设备 → 不启动
 *   - 粒子上限 MAX_PARTICLES；空闲无移动自动停 rAF，移动再启
 *
 * 回退：删 index.html 里 <script src="/effects.js"> 一行即可，零副作用。
 * ============================================================ */
(function () {
  'use strict';

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (window.matchMedia && window.matchMedia('(hover: none)').matches) return;

  var MAX_PARTICLES = 120;
  var SPAWN_PER_MOVE = 2;
  var IDLE_STOP_MS = 400;

  var canvas = document.createElement('canvas');
  canvas.id = '_fxTrail';
  canvas.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:60;';
  var ctx = canvas.getContext('2d');

  var dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  function resize() {
    canvas.width = Math.floor(window.innerWidth * dpr);
    canvas.height = Math.floor(window.innerHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  var particles = [];
  var running = false;
  var rafId = 0;
  var lastMoveAt = 0;

  // 适配浅色背景：饱和度高、明度中等的蓝→青→紫，白底上用 lighter 叠出光晕
  var HUES = [210, 225, 255, 275, 190];

  function spawn(x, y) {
    for (var i = 0; i < SPAWN_PER_MOVE; i++) {
      if (particles.length >= MAX_PARTICLES) particles.shift();
      var angle = Math.random() * Math.PI * 2;
      var speed = 0.4 + Math.random() * 1.4;
      particles.push({
        x: x, y: y,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed - 0.3,
        life: 1,
        decay: 0.012 + Math.random() * 0.02,
        size: 1.2 + Math.random() * 2.2,
        hue: HUES[(Math.random() * HUES.length) | 0]
      });
    }
  }

  function tick() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.globalCompositeOperation = 'lighter';

    for (var i = particles.length - 1; i >= 0; i--) {
      var p = particles[i];
      p.vy += 0.02;
      p.vx *= 0.98;
      p.x += p.vx;
      p.y += p.vy;
      p.life -= p.decay;
      if (p.life <= 0) { particles.splice(i, 1); continue; }

      var alpha = Math.max(0, p.life);
      var r = p.size * (0.6 + p.life * 0.8);
      var grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 3);
      grad.addColorStop(0, 'hsla(' + p.hue + ',95%,58%,' + (alpha * 0.85) + ')');
      grad.addColorStop(1, 'hsla(' + p.hue + ',95%,52%,0)');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r * 3, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalCompositeOperation = 'source-over';

    if (particles.length === 0 && (performance.now() - lastMoveAt) > IDLE_STOP_MS) {
      running = false;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    rafId = requestAnimationFrame(tick);
  }

  function start() {
    if (running) return;
    running = true;
    rafId = requestAnimationFrame(tick);
  }

  function onMove(e) {
    lastMoveAt = performance.now();
    spawn(e.clientX, e.clientY);
    start();
  }

  function init() {
    (document.body || document.documentElement).appendChild(canvas);
    resize();
    window.addEventListener('resize', resize, { passive: true });
    window.addEventListener('mousemove', onMove, { passive: true });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden && rafId) {
        cancelAnimationFrame(rafId);
        running = false;
        particles.length = 0;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
      }
    });
  }

  if (document.body) init();
  else document.addEventListener('DOMContentLoaded', init);
})();

/* ============================================================
 * effects.js — 鼠标拖尾特效（子应用版，简洁 + 跟随 Portal 开关）
 * ------------------------------------------------------------
 * 全屏透明 canvas（pointer-events:none 不挡点击），监听 mousemove
 * 在指针处落下柔和的淡色小点，快速淡出，拖出一条低调的尾巴。
 *
 * 与 Portal 的关系（关键机制）：
 *   - 这份跑在 iframe 子应用里。mousemove 事件只发给鼠标当前所在的
 *     document——鼠标在子应用工作区移动时，事件进的是 iframe 内部、
 *     不冒泡到 Portal 父页。所以工作区的拖尾必须由子应用自己画。
 *   - 开关状态与 Portal 共用同一个 localStorage key（portal_fx_trail）。
 *     Portal 与子应用同源，父页改这个 key 时，浏览器会给同源的 iframe
 *     派发 'storage' 事件，这里据此实时开/关，无需子应用内再放按钮。
 *
 * 简洁：source-over 正常绘制（无 lighter 发光叠加）、单一柔和灰蓝、
 *   低透明度、无四散初速度、原地快速淡出——不再晃眼。
 *
 * 性能护栏：
 *   - prefers-reduced-motion: reduce / 触摸设备 → 不启动
 *   - 粒子上限 MAX_PARTICLES；空闲无移动自动停 rAF，移动再启
 *
 * 回退：删 index.html 里 <script src="effects.js"> 一行即可，零副作用。
 * ============================================================ */
(function () {
  'use strict';

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (window.matchMedia && window.matchMedia('(hover: none)').matches) return;

  var FX_KEY = 'portal_fx_trail';
  var MAX_PARTICLES = 60;
  var IDLE_STOP_MS = 300;

  // 单一柔和中性色（slate-400），白底/深底都不刺眼；低透明度取代发光
  var TRAIL_RGB = '148,163,184';
  var BASE_ALPHA = 0.35;

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
  var enabled = (localStorage.getItem(FX_KEY) || 'on') !== 'off';

  function spawn(x, y) {
    if (particles.length >= MAX_PARTICLES) particles.shift();
    particles.push({
      x: x, y: y,
      life: 1,
      decay: 0.05 + Math.random() * 0.03,
      size: 2.5 + Math.random() * 1.5
    });
  }

  function tick() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (var i = particles.length - 1; i >= 0; i--) {
      var p = particles[i];
      p.life -= p.decay;
      if (p.life <= 0) { particles.splice(i, 1); continue; }
      var r = p.size * p.life;
      ctx.fillStyle = 'rgba(' + TRAIL_RGB + ',' + (p.life * BASE_ALPHA) + ')';
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();
    }

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

  function enableTrail() {
    window.addEventListener('mousemove', onMove, { passive: true });
  }

  function disableTrail() {
    window.removeEventListener('mousemove', onMove);
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    running = false;
    particles.length = 0;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  function applyEnabled(next) {
    if (next === enabled) return;
    enabled = next;
    if (enabled) enableTrail(); else disableTrail();
  }

  function init() {
    (document.body || document.documentElement).appendChild(canvas);
    resize();
    window.addEventListener('resize', resize, { passive: true });

    // 跟随 Portal 顶栏开关：父页改 portal_fx_trail 时，同源 iframe 收到 storage 事件
    window.addEventListener('storage', function (e) {
      if (e.key !== FX_KEY) return;
      applyEnabled((e.newValue || 'on') !== 'off');
    });

    document.addEventListener('visibilitychange', function () {
      if (document.hidden && rafId) {
        cancelAnimationFrame(rafId);
        rafId = 0;
        running = false;
        particles.length = 0;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
      }
    });

    if (enabled) enableTrail();
  }

  if (document.body) init();
  else document.addEventListener('DOMContentLoaded', init);
})();

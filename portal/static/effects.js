/* ============================================================
 * effects.js — 鼠标拖尾特效（Portal，简洁版）
 * ------------------------------------------------------------
 * 全屏透明 canvas（pointer-events:none 不挡点击），监听 mousemove
 * 在指针处落下柔和的淡色小点，快速淡出，拖出一条低调的尾巴。
 *
 * 为什么是「简洁」：
 *   - 旧版用 globalCompositeOperation='lighter'（叠加发光）+ 高饱和
 *     蓝紫青 + 粒子四散喷发，白底上很晃眼。现改为 source-over 正常
 *     绘制、单一柔和中性色、低透明度、无四散初速度 —— 只是淡淡地跟着
 *     光标走，快速消失。
 *
 * 可开关：
 *   - 页眉右侧加一个小按钮，点一下开/关，状态存 localStorage
 *     （key: portal_fx_trail，默认开）。关掉后不监听 mousemove、清空
 *     画布，零开销。
 *
 * 性能护栏：
 *   - prefers-reduced-motion: reduce / 触摸设备 → 连按钮都不放（无拖尾语义）
 *   - 粒子上限 MAX_PARTICLES；空闲无移动自动停 rAF，移动再启
 *
 * 回退：删 index.html 里 <script src="/effects.js"> 一行即可，零副作用。
 * ============================================================ */
(function () {
  'use strict';

  // 减少动态效果 / 无 hover（触摸）设备：不启动、也不放开关按钮
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (window.matchMedia && window.matchMedia('(hover: none)').matches) return;

  var FX_KEY = 'portal_fx_trail';
  var MAX_PARTICLES = 60;      // 简洁 = 少而淡
  var SPAWN_PER_MOVE = 1;      // 每次移动只落一个点
  var IDLE_STOP_MS = 300;

  // 单一柔和中性色（slate-400 系），白底/深底都不刺眼；用低透明度取代发光
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
      decay: 0.05 + Math.random() * 0.03,   // 快速淡出（~15 帧内消失）
      size: 2.5 + Math.random() * 1.5
    });
  }

  function tick() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    // 正常绘制（无 lighter 叠加发光）—— 这是「不晃眼」的关键
    for (var i = particles.length - 1; i >= 0; i--) {
      var p = particles[i];
      p.life -= p.decay;
      if (p.life <= 0) { particles.splice(i, 1); continue; }
      var r = p.size * p.life;               // 越淡越小，收成一个点
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

  // ---- 页眉开关按钮 ----
  function makeToggle() {
    var btn = document.createElement('button');
    btn.id = '_fxTrailToggle';
    btn.type = 'button';
    btn.textContent = '✦';
    function paint() {
      btn.title = enabled ? '鼠标拖尾：开（点击关闭）' : '鼠标拖尾：关（点击开启）';
      btn.style.cssText =
        'font-size:12px;line-height:1;padding:3px 8px;border-radius:5px;cursor:pointer;' +
        'background:transparent;color:' + (enabled ? '#e2e8f0' : '#64748b') + ';' +
        'border:1px solid ' + (enabled ? '#475569' : '#334155') + ';' +
        'opacity:' + (enabled ? '1' : '0.6') + ';transition:opacity .15s,color .15s;';
    }
    btn.addEventListener('click', function () {
      enabled = !enabled;
      localStorage.setItem(FX_KEY, enabled ? 'on' : 'off');
      if (enabled) enableTrail(); else disableTrail();
      paint();
    });
    paint();

    // 放到页眉右侧控制区（.portal-bar 的最后一个子元素）里，退出按钮之前
    var controls = document.querySelector('.portal-bar > div:last-child');
    if (controls) controls.insertBefore(btn, controls.firstChild);
    else (document.body || document.documentElement).appendChild(btn);
  }

  function init() {
    (document.body || document.documentElement).appendChild(canvas);
    resize();
    window.addEventListener('resize', resize, { passive: true });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden && rafId) {
        cancelAnimationFrame(rafId);
        rafId = 0;
        running = false;
        particles.length = 0;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
      }
    });

    makeToggle();
    if (enabled) enableTrail();
  }

  if (document.body) init();
  else document.addEventListener('DOMContentLoaded', init);
})();
